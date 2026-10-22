#!/data/data/com.termux/files/usr/bin/python3
"""One-time migration: retain existing passwords, write only salted hashes."""
import argparse
import ast
import os
from pathlib import Path
import sys

lib = Path.home()/'.local/lib/mobile-server'
sys.path.insert(0, str(lib if lib.exists() else Path(__file__).resolve().parents[1]/'app'))
from auth import hash_password, save_users, verify_password

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', required=True, type=Path)
p.add_argument('--destination', default=Path.home()/'.config/dashboard/users.json', type=Path)
a = p.parse_args()
if a.destination.exists():
    p.error('Destination already exists; refusing to overwrite password hashes')
tree = ast.parse(a.source.read_text())
legacy = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'USERS' for t in node.targets):
        legacy = ast.literal_eval(node.value)
        break
if not isinstance(legacy, dict) or not legacy or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in legacy.items()):
    p.error('Source does not contain the expected legacy user dictionary')
users = {name: {'password_hash': hash_password(password), 'role': 'admin' if name == 'astro' else 'family'} for name, password in legacy.items()}
assert all(verify_password(password, users[name]['password_hash']) for name, password in legacy.items())
save_users(a.destination, users)
print('Migrated', len(users), 'accounts to private salted password hashes; existing passwords still work.')
