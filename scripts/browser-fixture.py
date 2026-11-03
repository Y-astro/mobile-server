"""Copy maintained web templates to .work test fixtures without importing runtime server state."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
work = root / '.work'
work.mkdir(exist_ok=True)

shutil.copy2(root / 'web/admin.html', work / 'dashboard-test.html')
shutil.copy2(root / 'web/family.html', work / 'family-test.html')
shutil.copy2(root / 'web/login.html', work / 'login-test.html')
