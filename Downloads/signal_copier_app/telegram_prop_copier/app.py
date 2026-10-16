"""
Signal Copier dashboard.

    python app.py                     -> opens http://127.0.0.1:8765 in your browser
    python app.py --port 9000 --no-browser

Runs only on this computer (bound to 127.0.0.1). Keep this window open while copying.
"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from server import main
    main()
