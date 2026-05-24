"""Build-time flags baked into the bytecode at image-build time.

This file's value in the source tree is the **dev default**: it permits the
`FHIR_SDC_LICENSE_SKIP=1` escape hatch so local development and the test
suite can run without minting a real license.

The release Dockerfile overwrites this file with ``ALLOW_LICENSE_SKIP = False``
*before* ``compileall``, so the published image's ``.pyc`` is frozen with the
flag off — and the ``.py`` source is then deleted. At runtime the env var
becomes a no-op: the bytecode never consults it.
"""

ALLOW_LICENSE_SKIP = True
