"""Policy gates shared by format writers, archives and delivery adapters."""
from __future__ import annotations

from functools import wraps
from pathlib import Path
import hashlib
import tempfile
import zipfile
import shutil

from .artifacts import IntegrityError, derive, inspect, seal, sidecar, canonical
from .policy import ExportPurpose
from .query import require_query


def _attest_generated(artifact, purpose):
    # Called only after this process has rechecked EVERY actual input. This
    # ephemeral receipt cannot approve a new source and is not read from data.
    if purpose == ExportPurpose.DISTRIBUTION:
        from . import trusted_config
        artifact.lineage.check(purpose)
        trusted_config.DISTRIBUTION_ATTESTATIONS[artifact.sha256] = hashlib.sha256(
            canonical(artifact.lineage.payload()).encode()).hexdigest()
    return artifact


def guarded_conversion(formatter):
    """Retain format code; guard ALL public MT writer calls, including direct ones."""
    @wraps(formatter)
    def export(con, source_select, out_path, *args, purpose=ExportPurpose.LOCAL_TEST, **kwargs):
        lineage = require_query(source_select, purpose)
        out_path = Path(out_path)
        if out_path.exists() or sidecar(out_path).exists():
            raise FileExistsError("Choose a new MT output path")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out_path.parent) as td:
            temporary = Path(td) / out_path.name
            result = formatter(con, source_select, temporary, *args, **kwargs)
            require_query(source_select, purpose)
            import shutil
            with temporary.open("rb") as src, out_path.open("xb") as dest:
                shutil.copyfileobj(src, dest)
        _attest_generated(seal(out_path, lineage), purpose)
        return result
    return export


def build_zip(files, out_path, *, purpose=ExportPurpose.DISTRIBUTION, password=None):
    """Recheck actual bytes immediately before archiving. No unchecked ZIP fallback."""
    artifacts = [(inspect(path), arc) for path, arc in files]
    if not artifacts:
        raise IntegrityError("No archive members")
    for artifact, arc in artifacts:
        artifact.check(purpose)
        if Path(arc).is_absolute() or ".." in Path(arc).parts:
            raise IntegrityError("Invalid archive member")
    lineage = derive(a.lineage for a, _ in artifacts)
    lineage.check(purpose)
    out_path = Path(out_path)
    if out_path.exists() or sidecar(out_path).exists():
        raise FileExistsError("Choose a new ZIP output path")
    with tempfile.TemporaryDirectory() as td:
        temporary = Path(td) / "archive.zip"
        if password is None:
            archive = zipfile.ZipFile(temporary, "x", zipfile.ZIP_DEFLATED)
        else:
            import pyzipper
            archive = pyzipper.AESZipFile(temporary, "x", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES)
            archive.setpassword(password.encode())
        with archive as zf:
            for index, (artifact, arc) in enumerate(artifacts):
                artifact.check(purpose)
                # Snapshot then verify the bytes actually being archived.
                snapshot = Path(td) / f"member-{index}{artifact.path.suffix}"
                shutil.copyfile(artifact.path, snapshot)
                shutil.copyfile(sidecar(artifact.path), sidecar(snapshot))
                selected = inspect(snapshot)
                if selected.lineage != artifact.lineage or selected.sha256 != artifact.sha256:
                    raise IntegrityError("ZIP member changed")
                selected.check(purpose)
                zf.write(snapshot, arcname=arc)
                selected.check(purpose)
                artifact.check(purpose)
                zf.writestr(arc + ".provenance.json", sidecar(snapshot).read_bytes())
        for artifact, _ in artifacts:
            artifact.check(purpose)
        # A denied/changed input never leaves a final archive at the requested path.
        with temporary.open("rb") as src, out_path.open("xb") as dest:
            shutil.copyfileobj(src, dest)
    return _attest_generated(seal(out_path, lineage), purpose)


def download_payload(artifact):
    """Call immediately before exposing bytes through Streamlit or another UI."""
    artifact.check(ExportPurpose.DISTRIBUTION)
    data = artifact.path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise IntegrityError("Download content changed")
    artifact.check(ExportPurpose.DISTRIBUTION)
    return data


def streamlit_download(st, artifact, **kwargs):
    return st.download_button(data=download_payload(artifact), file_name=artifact.path.name,
                              mime="application/zip", **kwargs)
