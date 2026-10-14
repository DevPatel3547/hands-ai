# main.py -- Hands boot shim (do not edit). Starts the bench current monitor, then runs
# the generated program, which Hands stored as app.py.
try:
    import hands_bench
    hands_bench.start()
except Exception as e:  # the program still runs without current monitoring
    print("#INA OFF", e)
import app  # noqa: E402,F401
