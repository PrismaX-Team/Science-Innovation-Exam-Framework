"""Fail the image build if the shared baseline or the GPU stack moved."""
import importlib
import sys

BASELINE = {
    "numpy": "2.3.2",
    "pandas": "2.2.3",
    "scipy": "1.17.1",
    "sklearn": "1.7.2",
    "matplotlib": "3.10.8",
    "PIL": "12.3.0",
    "h5py": "3.16.0",
    "yaml": "6.0.2",
    "tqdm": "4.70.1",
    "joblib": "1.6.0",
}


def main():
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("Python 3.11 is required, found %s" % sys.version)
    for name, expected in BASELINE.items():
        module = importlib.import_module(name)
        actual = getattr(module, "__version__", None)
        if actual != expected:
            raise SystemExit("%s %s != %s" % (name, actual, expected))
    if "--gpu" in sys.argv:
        import torch
        if torch.__version__ != "2.8.0+cu128":
            raise SystemExit("torch %s != 2.8.0+cu128" % torch.__version__)
        if not torch.cuda.is_available() and "--require-cuda-device" in sys.argv:
            raise SystemExit("CUDA device is not visible")
    print("sie baseline selftest passed")


if __name__ == "__main__":
    main()
