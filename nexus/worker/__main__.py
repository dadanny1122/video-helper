"""python -m nexus.worker [--headless --master https://… --pair 123456 --name pc]"""
import sys

from nexus.worker.tray import main

sys.exit(main())
