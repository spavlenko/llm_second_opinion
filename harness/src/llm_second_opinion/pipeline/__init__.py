"""The task pipeline: import benchmark instances, build arm64 images, validate, freeze a manifest.

`bench tasks import` writes a candidates file, `bench tasks build` builds one image per
candidate, and `bench tasks validate` runs the gold patch twice and writes the frozen manifest.
"""
