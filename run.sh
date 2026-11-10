#!/bin/bash

OMADA_USER="$(pass show Pessoal/omada/user)"
OMADA_PASS="$(pass show Pessoal/omada/password)"

export OMADA_USER OMADA_PASS

python3 ./omada_ifstat.py --table
