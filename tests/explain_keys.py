"""Every explain.json key the dashboard can ask for: literal ones in index.html,
the ones it builds at runtime, and the ones web.py's panels emit. Shared by the
test and by whoever adds a panel."""

import json
import os
import re
import sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP)


def referenced_keys():
    import lineage
    import market_context as MC
    import market_trend as MT
    import regime_equity as RE
    import screens as SN
    with open(os.path.join(APP, "static", "index.html")) as f:
        page = f.read()
    keys = {k for k in re.findall(r'infoBtn\("([A-Za-z0-9_]+)"', page) if not k.endswith("_")}
    header = re.search(r"const HEADER_KEYS = \{(.*?)\};", page, re.S).group(1)
    keys |= set(re.findall(r':\s*"([A-Za-z0-9_]+)"', header))                         # column headers
    keys |= set(re.findall(r'data-g="([a-z_]+)"', page))                                  # panel headings
    trk = re.search(r"const TRK_COLS = \[(.*?)\];", page, re.S).group(1)
    keys |= {"t_" + k for k in re.findall(r'\["([a-z0-9_]+)",', trk)}                    # tracker columns
    keys |= {"t_screen_" + k for k in SN.SCREENS}                                         # tracker screens
    keys |= {"st_" + s for s in lineage.LADDER + [lineage.TERMINAL]}                      # lineage statuses
    keys |= {"pat_" + s.lower().replace(" ", "_") for s in
             ("SUPPORTED", "SUGGESTIVE", "NOT SUPPORTED", "OPPOSITE", "TOO FEW")}         # pattern verdicts
    keys |= {s.lower() for s in ("Bull", "Flat", "Bear")}                                 # trend words
    keys |= {f"A{i}" for i in range(1, 10)}                                               # adversary attacks
    keys |= {row[0] for row in MC.INDICATORS}                                             # web.py: context rows
    with open(os.path.join(APP, "market_context.py")) as f:
        keys |= set(re.findall(r'"key": "([a-z0-9_]+)"', f.read()))                       # web.py: derived rows
    keys |= set(RE.STATES)                                                                # web.py: regime tag
    del MT
    return keys


def explain():
    with open(os.path.join(APP, "static", "explain.json")) as f:
        return json.load(f)
