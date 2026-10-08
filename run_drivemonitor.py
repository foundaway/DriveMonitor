"""Punto de entrada para ejecutar desde el código fuente o empaquetar con PyInstaller."""

import sys

from drivemonitor.app import main

if __name__ == "__main__":
    sys.exit(main())
