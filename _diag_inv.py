"""Diagnostic: run Azure inventory against the live az-cli session."""
import time
import azure_core as azure

cred = azure.get_credential(mode="cli")

subs = azure.discover_subscriptions(cred)
print("=== SUBSCRIPTIONS ===")
print(subs.to_string())
print()

for sub in subs.to_dict("records"):
    sid = sub["subscriptionId"]
    name = sub["displayName"]
    print(f"=== VMs in {name} ({sid}) ===")
    t0 = time.time()
    try:
        df = azure.inventory_vms(cred, sid)
        dt = time.time() - t0
        print(f"rows={len(df)}  elapsed={dt:.1f}s  note={df.attrs.get('note')}")
        if not df.empty:
            print(df[["name", "resourceGroup", "location", "powerState"]].to_string())
    except Exception as e:  # noqa: BLE001
        print(f"EXCEPTION: {type(e).__name__}: {e}")
    print()
