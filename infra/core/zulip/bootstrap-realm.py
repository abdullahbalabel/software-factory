# Fresh factory only (infra/factory.sh install): the Zulip organisation, idempotent. Fed to
# `manage.py shell` inside the zulip container; parameters in the environment:
#   FACTORY_REALM_NAME, FACTORY_OWNER_EMAIL, FACTORY_OWNER_NAME, FACTORY_BOT_EMAIL, FACTORY_STREAMS (JSON
#   [[name, description], ...]).
# Creates the root organisation, the owner (organisation owner, no password: he sets his own through the one-time
# link of owner-link.sh), the supervisor bot (generic bot owned by the owner), the streams with owner and bot
# subscribed, and requires end-to-end encrypted push. Prints one line BOT_API_KEY=<key> for the caller, which stores
# it in /srv/factory/secrets without showing it.
import json
import os

from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.realm_settings import do_set_realm_property
from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Realm, UserProfile

env = os.environ
realm = Realm.objects.filter(string_id="").first()
if realm is None:
    realm = do_create_realm(string_id="", name=env["FACTORY_REALM_NAME"])
    print("created organisation", realm.name)
if not realm.require_e2ee_push_notifications:
    do_set_realm_property(realm, "require_e2ee_push_notifications", True, acting_user=None)

owner = UserProfile.objects.filter(realm=realm, delivery_email__iexact=env["FACTORY_OWNER_EMAIL"]).first()
if owner is None:
    owner = do_create_user(env["FACTORY_OWNER_EMAIL"], None, realm, env["FACTORY_OWNER_NAME"],
                           role=UserProfile.ROLE_REALM_OWNER, acting_user=None)
    print("created owner", owner.delivery_email)

bot = UserProfile.objects.filter(realm=realm, delivery_email__iexact=env["FACTORY_BOT_EMAIL"]).first()
if bot is None:
    bot = do_create_user(env["FACTORY_BOT_EMAIL"], None, realm, "Factory supervisor",
                         bot_type=UserProfile.DEFAULT_BOT, bot_owner=owner, acting_user=owner)
    print("created bot", bot.delivery_email)

streams = []
for name, description in json.loads(env["FACTORY_STREAMS"]):
    stream, created = create_stream_if_needed(realm, name, stream_description=description, acting_user=owner)
    if created:
        print("created stream", name)
    streams.append(stream)
bulk_add_subscriptions(realm, streams, [owner, bot], acting_user=owner)
print("BOT_API_KEY=" + bot.api_key)
