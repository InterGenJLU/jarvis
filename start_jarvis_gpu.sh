#!/bin/bash
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export ROCM_PATH=/opt/rocm-7.2.0
export LD_LIBRARY_PATH=/opt/rocm-7.2.0/lib:$LD_LIBRARY_PATH

exec /usr/bin/python3 /home/user/jarvis/jarvis_continuous.py
