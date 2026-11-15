"""Basic-phone channel: USSD menus, SMS and mobile-money payments.

Shaped like the real services so the code can switch to them without changes:
  ussd.py         Africa's Talking USSD callback (sessionId, phoneNumber, text -> CON/END)
  sms_gateway.py  simulated outbox, or the Africa's Talking SMS API (sandbox or live)
  momo.py         simulated MTN MoMo, or the MTN MoMo Collections sandbox (request to pay)
  orders.py       paid services: start a payment, check it, deliver the SMS when paid
  advice.py       hosted rotation engine results for these channels (cached)
  store.py        small JSON stores in data/channels/ (atomic, locked)
  routes.py       FastAPI routes, including the phone simulator's helper endpoints
"""
