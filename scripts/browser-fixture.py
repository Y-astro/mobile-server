"""Extract the real dashboard HTML without importing runtime server state."""
import ast
from pathlib import Path
root = Path(__file__).resolve().parents[1]
tree = ast.parse((root/'app/dashboard.py').read_text())
html = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'ADMIN_DASHBOARD_HTML' for target in node.targets))
(root/'.work').mkdir(exist_ok=True)
(root/'.work/dashboard-test.html').write_text(html)
