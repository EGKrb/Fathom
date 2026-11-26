# Copies next to the given modules the DLLs they import, recursively, from the given directories (MSYS2's bin),
# leaving out Windows' own. Usage: collect-dlls.py <destination> <search dir;...> <module> [<module> ...]
import os
import re
import shutil
import subprocess
import sys

destination, search, modules = sys.argv[1], sys.argv[2].split(';'), sys.argv[3:]


def imports(path):
    out = subprocess.run(['objdump', '-p', path], capture_output=True, text=True).stdout
    return re.findall(r'DLL Name: (\S+)', out)


seen, copied = set(), []
queue = list(modules)
while queue:
    module = queue.pop()
    for name in imports(module):
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        for directory in search:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                target = os.path.join(destination, name)
                if not os.path.exists(target):
                    shutil.copy2(candidate, target)
                    copied.append(name)
                queue.append(candidate)
                break
print('copied:', ' '.join(sorted(copied)) or '(nothing)')
