import os, sys, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="dsp_state_")
os.environ["DHAN_CLIENT_ID"] = "test"
os.environ["DHAN_ACCESS_TOKEN"] = "test"
