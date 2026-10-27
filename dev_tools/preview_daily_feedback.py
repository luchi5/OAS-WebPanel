"""Loopback-only visual fixture; every report and account is synthetic."""
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from auth import AuthStore
from tests.test_backend import FakeOas
from tests.test_daily_feedback import FeedbackFixture
from daily_feedback_reader import DailyFeedbackReader


def main():
    import uvicorn
    fixture = FeedbackFixture()
    fixture.setup_fixture()
    day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    fixture.config('01-自己', collective=False)
    fixture.config('02-托管至10-24')
    fixture.report('02-托管至10-24', day=day)
    fixture.config('03-托管10-30')
    fixture.report('03-托管10-30', day=day, count=25)
    fixture.config('04-常规托管至11-1', collective=False)
    fixture.report('04-常规托管至11-1', day=day, phase='provisional')
    fixture.config('09', talisman=False, collective=False)
    fixture.config('10', talisman=False, collective=False)
    fixture.config('11', talisman=False, collective=False)
    settings = {'host': '127.0.0.1', 'port': 22302, 'public_origin': 'https://panel.example.com:4443',
                'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(fixture.root),
                'state_dir': str(fixture.root / 'state'), 'authentication_required': False}
    try:
        uvicorn.run(create_app(settings, client=FakeOas(), auth=AuthStore(fixture.root / 'state')),
                    host='127.0.0.1', port=22302, access_log=False)
    finally:
        fixture.temp.cleanup()


if __name__ == '__main__':
    main()
