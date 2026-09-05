import unittest
from phase3d_load import measure


class MultiCollectorTests(unittest.TestCase):
    def check_size(self,count):
        result=measure(count)
        self.assertEqual(result['authenticated_heartbeats'],4*count)
        self.assertEqual(result['notification_deliveries'],2*count)
        self.assertEqual(result['restart_restored_nodes'],count)
        self.assertEqual(result['pending_after_dispatch'],0)

    def test_ten_collectors(self): self.check_size(10)
    def test_twenty_collectors(self): self.check_size(20)
    def test_fifty_collectors(self): self.check_size(50)


if __name__=='__main__': unittest.main()
