# QR Lead Capture SaaS

Turn walk-ins into repeat customers with QR code lead capture.

## Quick Deploy to Render

### 1. Stripe Setup (5 min)
1. Go to [Stripe Dashboard](https://dashboard.stripe.com)
2. Create a Product:
   - Name: "QR Lead Capture Monthly"
   - Price: $49/month (recurring)
   - Copy the Price ID (starts with `price_`)
3. Get your API keys from Developers → API Keys
4. Set up Webhook:
   - Endpoint: `https://YOUR-APP.onrender.com/webhook/stripe`
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
   - Copy the Webhook signing secret

### 2. Deploy to Render
1. Push this repo to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com)
3. New → Blueprint → Connect your repo
4. Render will create the database and web service

### 3. Set Environment Variables
In Render dashboard, set these for the web service:

```
STRIPE_SECRET_KEY=sk_live_xxx (or sk_test_xxx for testing)
STRIPE_PUBLISHABLE_KEY=pk_live_xxx (or pk_test_xxx)
STRIPE_WEBHOOK_SECRET=whsec_xxx
STRIPE_PRICE_ID=price_xxx
BASE_URL=https://your-app.onrender.com
MAIL_USERNAME=your-smtp2go-username
MAIL_PASSWORD=your-smtp2go-password
MAIL_FROM=leads@yourdomain.com
```

### 4. Test the Flow
1. Visit your app URL
2. Click "Start Free Trial"
3. Fill in venue info
4. Complete Stripe checkout (use test card 4242424242424242)
5. You should land on success page with your QR code!

## Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SECRET_KEY=dev-secret
export DATABASE_URL=sqlite:///leads.db
export STRIPE_SECRET_KEY=sk_test_xxx
export STRIPE_PUBLISHABLE_KEY=pk_test_xxx
export STRIPE_PRICE_ID=price_xxx
export BASE_URL=http://localhost:5000

# Run
flask run
```

## Architecture

- **Flask** - Web framework
- **PostgreSQL** - Database (SQLite for local dev)
- **Stripe** - Payments & subscriptions
- **SMTP2Go** - Email notifications

## Files

```
├── app.py              # Main application
├── models.py           # Database models
├── templates/
│   ├── base.html       # Base template
│   ├── landing.html    # Marketing homepage
│   ├── signup.html     # Venue signup form
│   ├── capture.html    # Customer-facing QR page
│   ├── dashboard.html  # Venue owner dashboard
│   ├── settings.html   # Venue settings
│   └── email/          # Email templates
├── render.yaml         # Render deployment config
└── requirements.txt    # Python dependencies
```

## Revenue Model

- $49/month per venue
- 7-day free trial
- Stripe handles all billing, invoices, cancellations

## Support

Built by Von Base Enterprises
