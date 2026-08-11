import os
from twilio.rest import Client

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_NUMBER = os.environ.get("TWILIO_FROM")
TO_NUMBER = os.environ.get("TWILIO_TO")

print("Account SID :", ACCOUNT_SID)
print("Auth Token  :", "Loaded" if AUTH_TOKEN else "Missing")
print("From Number :", FROM_NUMBER)
print("To Number   :", TO_NUMBER)

if not all([ACCOUNT_SID, AUTH_TOKEN, FROM_NUMBER, TO_NUMBER]):
    raise Exception("One or more environment variables are missing.")

client = Client(ACCOUNT_SID, AUTH_TOKEN)

try:
    message = client.messages.create(
        body="Hello! This is a Twilio SMS test.",
        from_=FROM_NUMBER,
        to=TO_NUMBER
    )

    print("\nSMS request sent successfully!")
    print("Message SID :", message.sid)
    print("Status      :", message.status)

except Exception as e:
    print("\nSMS FAILED")
    print(type(e).__name__)
    print(e)