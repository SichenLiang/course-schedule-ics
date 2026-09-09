import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from courseics.cli import main
sys.exit(main())
