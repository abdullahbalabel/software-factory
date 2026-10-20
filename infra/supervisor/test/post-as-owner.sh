#!/bin/bash
# TEST ONLY. Post a Zulip message as the organisation owner, from the server side, to exercise the supervisor end to
# end without the owner's phone (the supervisor acts only on owner messages; a bot or test account cannot stand in).
# Uses Zulip's own message-sending code (manage.py shell, check_send_stream_message) inside the Zulip container, so
# the message goes through the normal event pipeline. Needs root on the VM. Messages carry the client name
# "factory-e2e-test" (Zulip records the sending client), so they stay distinguishable from the owner's own.
#
# The supervisor records these messages' decisions as "TEST, not the owner (build agent as <name>)": it needs both the
# client name and a fresh root-only mark (/var/lib/factory-test-marks/post, written just before posting, valid 120 s),
# so neither the owner's own messages nor anything sent through Zulip alone can be marked.
#
#   sudo infra/supervisor/test/post-as-owner.sh <stream> <topic> <text>
# Prints the new message id.
set -euo pipefail
[ $# -eq 3 ] || { echo "usage: $0 <stream> <topic> <text>" >&2; exit 2; }
export F_STREAM=$1 F_TOPIC=$2 F_TEXT=$3
install -d -o root -g root -m 755 /var/lib/factory-test-marks
touch /var/lib/factory-test-marks/post
cd /srv/factory/zulip
docker compose exec -T -e F_STREAM -e F_TOPIC -e F_TEXT -u zulip zulip \
  /home/zulip/deployments/current/manage.py shell -c '
import os
from zerver.actions.message_send import check_send_stream_message
from zerver.models import UserProfile
from zerver.models.clients import get_client
owner = UserProfile.objects.filter(role=UserProfile.ROLE_REALM_OWNER, is_bot=False, is_active=True).order_by("id").first()
mid = check_send_stream_message(owner, get_client("factory-e2e-test"), os.environ["F_STREAM"], os.environ["F_TOPIC"],
                                os.environ["F_TEXT"])
print(mid)
'
