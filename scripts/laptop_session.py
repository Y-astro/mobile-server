#!/data/data/com.termux/files/usr/bin/python3
"""ttyd URL argument is an ID, never a shell command."""
from pathlib import Path
import os
import sys
sys.path.insert(0,str(Path.home()/'.local/lib/mobile-server'))
from laptop import LaptopClient, LaptopError, validate_id
try:
    if len(sys.argv)!=2:raise LaptopError('Open a laptop terminal from the dashboard first',400)
    ident=validate_id(sys.argv[1])
    args=LaptopClient().ssh_args('attach '+ident,terminal=True)
    os.execvp(args[0],args)
except LaptopError as error:
    print(str(error))
    raise SystemExit(1)
