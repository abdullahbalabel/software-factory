#!/bin/bash
# Print a one-time "set your password" link for the owner's Zulip account (Django password-reset token: valid until
# used, until the password changes, or until PASSWORD_RESET_TIMEOUT). The owner opens it over Tailscale and chooses his
# own password; nobody else ever knows it. Run on the factory VM in /srv/factory/zulip.
# Usage: owner-link.sh [email]   (default: the owner account created with the organisation)
set -euo pipefail
EMAIL=${1:-}
cd /srv/factory/zulip
sudo docker compose exec -T -u zulip zulip /home/zulip/deployments/current/manage.py shell 2>/dev/null <<EOF | sed -n 's/^LINK=//p'
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from zerver.models import Realm, UserProfile
realm = Realm.objects.get(string_id="")
if "$EMAIL":
    u = UserProfile.objects.get(realm=realm, delivery_email="$EMAIL", is_active=True)
else:
    u = UserProfile.objects.filter(realm=realm, role=UserProfile.ROLE_REALM_OWNER, is_bot=False,
                                   is_active=True).order_by("id").first()
path = reverse("password_reset_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(u.id)), "token": default_token_generator.make_token(u)})
print("LINK=" + realm.url + path)
EOF
