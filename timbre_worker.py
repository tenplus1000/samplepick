"""AudioCommons timbral models on a few prepared WAVs, in parallel. Runs in the python named by
SAMPLEPICK_TIMBRE_PYTHON (the package needs numpy 1.x and librosa 0.9); called by analyze.py.

    $SAMPLEPICK_TIMBRE_PYTHON timbre_worker.py a.wav b.wav …   → one JSON line per file
"""
import json
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
    os.environ[_v] = "1"  # one thread per process: eight processes with eight threads each ran 4x slower
import numpy as np  # noqa: E402

for _a, _t in (("float", float), ("int", int), ("complex", complex), ("bool", bool)):
    setattr(np, _a, _t)  # the package still uses the aliases numpy removed


NAMES = {"hardness": "hardness", "depth": "depth", "brightness": "brightness", "roughness": "roughness",
         "warmth": "warmth", "sharpness": "sharpness", "boominess": "booming", "reverb": "reverb"}


def one(f: str) -> dict:
    warnings.filterwarnings("ignore")
    import timbral_models as tm
    try:
        r = {k: float(getattr(tm, "timbral_" + fn)(f)) for k, fn in NAMES.items()}
        return {"file": f, "timbre": {k: round(v, 1) for k, v in r.items()}}
    except Exception as e:  # a silent or odd file: no timbre, the rest still works
        return {"file": f, "error": repr(e)[:200]}


if __name__ == "__main__":
    with ProcessPoolExecutor() as ex:
        for r in ex.map(one, sys.argv[1:]):
            print(json.dumps(r), flush=True)
