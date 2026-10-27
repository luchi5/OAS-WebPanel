import tempfile
import unittest
from pathlib import Path
from weekly_metrics import read_weekly

class WeeklyTests(unittest.TestCase):
    def test_week_boundary_and_daily_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'log').mkdir()
            def write(day, messages):
                (root/'log'/f'{day}_test.txt').write_text(''.join(
                    f'{day} 12:00:{i:02d}.000 | {source}:001 | INFO | {message}\n'
                    for i,(source,message) in enumerate(messages)),encoding='utf-8')
            write('2026-10-11',[('daily_closeout.py','Weekly task completed: Dokan')])
            write('2026-10-12',[('daily_closeout.py','Weekly task completed: Dokan')]*2+
                  [('script.py','Scheduler: Start task `Chess`'),('economy.py','Chess coin: 230/600')])
            write('2026-10-13',[('script.py','Scheduler: Start task `Hunt`'),('script_task.py','Battle win'),
                               ('moon_sea.py','Double reward'),('moon_sea.py','Weekly realm chest consumed: 1')])
            data=read_weekly(root,'test','2026-10-13')
            self.assertEqual(data['Dokan']['value'],1)
            self.assertEqual(data['Kirin']['value'],1)
            self.assertEqual(data['Netherworld']['value'],0)
            self.assertEqual(data['Chess']['value'],230)
            self.assertEqual(data['SixRealms']['value'],1)
            self.assertEqual(data['Dokan']['start'],'2026-10-12')

    def test_missing_evidence_is_not_confirmed_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            data=read_weekly(Path(directory),'test','2026-10-14')
            self.assertFalse(data['Dokan']['verified'])
            self.assertIsNone(data['Chess']['value'])

if __name__=='__main__':unittest.main()
