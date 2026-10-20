#!/bin/bash
# TEST ONLY. Upload a file to Zulip as the organisation owner (server side, like post-as-owner.sh) and print the
# Markdown link to put in a message: [name](/user_uploads/…). Needs root on the VM.
#
#   sudo bash infra/supervisor/test/upload-as-owner.sh <local-file>
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 <file>" >&2; exit 2; }
export F_NAME F_DATA
F_NAME=$(basename "$1")
F_DATA=$(base64 -w0 "$1")
cd /srv/factory/zulip
docker compose exec -T -e F_NAME -e F_DATA -u zulip zulip \
  /home/zulip/deployments/current/manage.py shell -c '
import base64, mimetypes, os
from zerver.lib.upload import upload_message_attachment
from zerver.models import UserProfile
owner = UserProfile.objects.filter(role=UserProfile.ROLE_REALM_OWNER, is_bot=False, is_active=True).order_by("id").first()
name = os.environ["F_NAME"]
res = upload_message_attachment(name, mimetypes.guess_type(name)[0] or "application/octet-stream",
                                base64.b64decode(os.environ["F_DATA"]), owner)
url = res[0] if isinstance(res, tuple) else res
print(f"[{name}]({url})")
' 2>/dev/null | tail -1
