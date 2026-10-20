#!/bin/bash
# TEST ONLY. Add an emoji reaction as the organisation owner (server side, like post-as-owner.sh), e.g. ✅ on a plan:
#   sudo infra/supervisor/test/react-as-owner.sh <message-id> check
# It first writes the root-only mark /var/lib/factory-test-marks/reaction-<message-id>-<emoji>, so the supervisor
# records the approval or rejection as "TEST, not the owner (build agent as <name>)".
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <message-id> <emoji-name>" >&2; exit 2; }
export F_MSG=$1 F_EMOJI=$2
[[ $F_MSG =~ ^[0-9]+$ && $F_EMOJI =~ ^[a-z0-9_]+$ ]] || { echo "bad message id or emoji name" >&2; exit 2; }
install -d -o root -g root -m 755 /var/lib/factory-test-marks
touch "/var/lib/factory-test-marks/reaction-$F_MSG-$F_EMOJI"
cd /srv/factory/zulip
docker compose exec -T -e F_MSG -e F_EMOJI -u zulip zulip \
  /home/zulip/deployments/current/manage.py shell -c '
import os
from django.db import transaction
from zerver.actions.reactions import check_add_reaction
from zerver.models import UserProfile
owner = UserProfile.objects.filter(role=UserProfile.ROLE_REALM_OWNER, is_bot=False, is_active=True).order_by("id").first()
with transaction.atomic():
    check_add_reaction(owner, int(os.environ["F_MSG"]), os.environ["F_EMOJI"], None, None)
print("reacted")
'
