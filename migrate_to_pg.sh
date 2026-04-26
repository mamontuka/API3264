#!/bin/bash
su - postgres -c "cd /root/ai/core/qwen/api3264 && python3 migrate_to_pg.py"
