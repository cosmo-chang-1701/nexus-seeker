#!/bin/bash
chown -R appuser:appuser /app/data
exec gosu appuser "$@"
