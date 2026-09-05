"""Windows Task Scheduler XML generation only: never register or start a task."""
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import subprocess
import xml.etree.ElementTree as ET

from ..config import ConfigError, logical_id, path_text


@dataclass(frozen=True)
class CollectorTask:
    collector_id: str
    python_executable: str
    entry_script: str
    configuration_path: str
    working_directory: str

    def __post_init__(self):
        logical_id(self.collector_id)
        for value in (self.python_executable,self.entry_script,self.configuration_path,self.working_directory):
            path_text(value)
            if not PureWindowsPath(value).is_absolute():
                raise ConfigError('Task paths must be explicit absolute Windows paths')
        if PureWindowsPath(self.python_executable).suffix.lower()!='.exe' or PureWindowsPath(self.entry_script).suffix.lower()!='.py':
            raise ConfigError('Task requires Python executable and reviewed Python entry point')

    def xml(self):
        ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
        ET.register_namespace('',ns)
        def element(parent,tag,text=None,**attributes):
            value=ET.SubElement(parent,'{'+ns+'}'+tag,attributes)
            value.text=text
            return value
        root=ET.Element('{'+ns+'}Task',{'version':'1.2'})
        registration=element(root,'RegistrationInfo')
        element(registration,'Description','Collector '+self.collector_id+'; review configuration and privileges before enabling')
        triggers=element(root,'Triggers'); boot=element(triggers,'BootTrigger')
        element(boot,'Enabled','true'); element(boot,'Delay','PT30S')
        principals=element(root,'Principals'); principal=element(principals,'Principal',id='Collector')
        # Built-in LocalService SID is public OS metadata, not a broker/user account.
        element(principal,'UserId','S-1-5-19'); element(principal,'LogonType','ServiceAccount')
        element(principal,'RunLevel','LeastPrivilege')
        settings=element(root,'Settings')
        element(settings,'MultipleInstancesPolicy','IgnoreNew')
        element(settings,'DisallowStartIfOnBatteries','false'); element(settings,'StopIfGoingOnBatteries','false')
        element(settings,'StartWhenAvailable','true'); element(settings,'Enabled','false')
        element(settings,'ExecutionTimeLimit','PT0S')
        restart=element(settings,'RestartOnFailure'); element(restart,'Interval','PT1M'); element(restart,'Count','3')
        actions=element(root,'Actions',Context='Collector'); command=element(actions,'Exec')
        element(command,'Command',self.python_executable)
        element(command,'Arguments',subprocess.list2cmdline(['-B',self.entry_script,'--config',self.configuration_path]))
        element(command,'WorkingDirectory',self.working_directory)
        return ET.tostring(root,encoding='utf-8',xml_declaration=True)

    def write_new(self,path):
        with Path(path).open('xb') as target: target.write(self.xml())
