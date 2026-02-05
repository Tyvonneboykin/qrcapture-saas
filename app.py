# app.py - QR Lead Capture SaaS
# Dual payment processing: Stripe + PayPal for redundancy

import os
import stripe
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from flask_mail import Mail, Message
from functools import wraps
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# Database
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///leads.db')
# Render uses postgres:// but SQLAlchemy needs postgresql://
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

from models import db, Venue, Lead
db.init_app(app)

# Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID')  # Monthly subscription price
STRIPE_ENABLED = bool(os.environ.get('STRIPE_SECRET_KEY'))

# PayPal
PAYPAL_CLIENT_ID = os.environ.get('PAYPAL_CLIENT_ID')
PAYPAL_SECRET = os.environ.get('PAYPAL_SECRET')
PAYPAL_PLAN_ID = os.environ.get('PAYPAL_PLAN_ID')
PAYPAL_API_BASE = 'https://api-m.paypal.com'  # Use sandbox for testing
PAYPAL_ENABLED = bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)

# Email (for lead notifications)
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.smtp2go.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 2525))
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_FROM', 'leads@vonbase.com')
mail = Mail(app)

# Base URL for QR codes
BASE_URL = os.environ.get('BASE_URL', 'https://qrcapture.vonbase.com')

def get_paypal_access_token():
    """Get PayPal OAuth token"""
    response = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={'grant_type': 'client_credentials'}
    )
    return response.json().get('access_token')

# =============================================================================
# AUTH HELPERS
# =============================================================================

def venue_required(f):
    """Decorator to require venue login"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'venue_id' not in session:
            flash('Please log in to continue', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_current_venue():
    """Get the currently logged-in venue"""
    if 'venue_id' in session:
        return Venue.query.get(session['venue_id'])
    return None

# =============================================================================
# PUBLIC PAGES
# =============================================================================

@app.route('/')
def home():
    """Landing page - sell the product"""
    return render_template('landing.html')

@app.route('/pricing')
def pricing():
    """Pricing page"""
    return render_template('pricing.html', stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/privacy')
def privacy():
    """Privacy Policy"""
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    """Terms of Service"""
    return render_template('terms.html')

@app.route('/about')
def about():
    """About page"""
    return render_template('about.html')

# =============================================================================
# LEAD CAPTURE (The Core Product)
# =============================================================================

@app.route('/c/<slug>')
def capture_page(slug):
    """Customer-facing capture page - what people see when they scan QR"""
    venue = Venue.query.filter_by(slug=slug, active=True).first_or_404()
    
    # Check subscription is active
    if venue.subscription_status not in ('active', 'trialing'):
        return render_template('capture_inactive.html'), 402
    
    return render_template('capture.html', venue=venue)

@app.route('/c/<slug>/submit', methods=['POST'])
def capture_submit(slug):
    """Handle lead submission"""
    venue = Venue.query.filter_by(slug=slug, active=True).first_or_404()
    
    if venue.subscription_status not in ('active', 'trialing'):
        return jsonify({'error': 'Venue subscription inactive'}), 402
    
    # Get form data
    phone = request.form.get('phone', '').strip()
    email = request.form.get('email', '').strip()
    name = request.form.get('name', '').strip()
    
    # Require at least phone or email
    if not phone and not email:
        return jsonify({'error': 'Phone or email required'}), 400
    
    # Create lead
    lead = Lead(
        venue_id=venue.id,
        phone=phone or None,
        email=email or None,
        name=name or None,
        source='qr'
    )
    db.session.add(lead)
    db.session.commit()
    
    # Send notification email to venue
    try:
        send_lead_notification(venue, lead)
    except Exception as e:
        app.logger.error(f"Failed to send lead notification: {e}")
    
    return jsonify({
        'success': True,
        'message': venue.thank_you_message
    })

def send_lead_notification(venue, lead):
    """Email venue owner about new lead"""
    msg = Message(
        subject=f"🎯 New Lead Captured at {venue.name}!",
        recipients=[venue.email],
        html=render_template('email/new_lead.html', venue=venue, lead=lead)
    )
    mail.send(msg)

# =============================================================================
# STRIPE CHECKOUT & BILLING
# =============================================================================

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Venue signup - show payment options"""
    if request.method == 'GET':
        return render_template('signup.html', 
                             paypal_client_id=PAYPAL_CLIENT_ID,
                             paypal_plan_id=PAYPAL_PLAN_ID,
                             stripe_enabled=STRIPE_ENABLED,
                             paypal_enabled=PAYPAL_ENABLED)
    
    # POST = Stripe checkout (PayPal handled client-side)
    venue_name = request.form.get('venue_name', '').strip()
    email = request.form.get('email', '').strip()
    payment_method = request.form.get('payment_method', 'stripe')
    
    if not venue_name or not email:
        flash('Venue name and email required', 'error')
        return render_template('signup.html',
                             paypal_client_id=PAYPAL_CLIENT_ID,
                             paypal_plan_id=PAYPAL_PLAN_ID,
                             stripe_enabled=STRIPE_ENABLED,
                             paypal_enabled=PAYPAL_ENABLED)
    
    # Store in session for PayPal flow
    session['pending_venue_name'] = venue_name
    session['pending_email'] = email
    
    if payment_method == 'paypal':
        # PayPal subscription is created client-side, redirect to PayPal page
        return render_template('signup_paypal.html',
                             venue_name=venue_name,
                             email=email,
                             paypal_client_id=PAYPAL_CLIENT_ID,
                             paypal_plan_id=PAYPAL_PLAN_ID,
                             base_url=BASE_URL)
    
    # Stripe checkout
    if not STRIPE_ENABLED:
        flash('Stripe is not configured. Please use PayPal.', 'error')
        return redirect(url_for('signup'))
    
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f"{BASE_URL}/signup/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/signup?canceled=true",
            customer_email=email,
            metadata={
                'venue_name': venue_name,
                'email': email
            },
            subscription_data={
                'trial_period_days': 7,
                'metadata': {
                    'venue_name': venue_name
                }
            }
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        app.logger.error(f"Stripe checkout error: {e}")
        flash('Card payment unavailable. Please try PayPal.', 'error')
        return render_template('signup.html',
                             paypal_client_id=PAYPAL_CLIENT_ID,
                             paypal_plan_id=PAYPAL_PLAN_ID,
                             stripe_enabled=STRIPE_ENABLED,
                             paypal_enabled=PAYPAL_ENABLED)

@app.route('/signup/success')
def signup_success():
    """Post-checkout success page"""
    session_id = request.args.get('session_id')
    if not session_id:
        return redirect(url_for('home'))
    
    # Get checkout session to find venue
    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
        venue = Venue.query.filter_by(stripe_customer_id=checkout.customer).first()
        
        if venue:
            # Log them in
            session['venue_id'] = venue.id
            return render_template('signup_success.html', venue=venue, base_url=BASE_URL)
    except Exception as e:
        app.logger.error(f"Error retrieving checkout session: {e}")
    
    return render_template('signup_success.html', venue=None, base_url=BASE_URL)

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks - this is where the magic happens"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle events
    if event['type'] == 'checkout.session.completed':
        handle_checkout_completed(event['data']['object'])
    elif event['type'] == 'customer.subscription.updated':
        handle_subscription_updated(event['data']['object'])
    elif event['type'] == 'customer.subscription.deleted':
        handle_subscription_deleted(event['data']['object'])
    elif event['type'] == 'invoice.payment_failed':
        handle_payment_failed(event['data']['object'])
    
    return jsonify({'received': True})

def handle_checkout_completed(checkout_session):
    """Create venue when checkout completes"""
    metadata = checkout_session.get('metadata', {})
    
    # Check if venue already exists for this customer
    existing = Venue.query.filter_by(stripe_customer_id=checkout_session['customer']).first()
    if existing:
        return
    
    # Create new venue
    venue = Venue(
        name=metadata.get('venue_name', 'New Venue'),
        email=metadata.get('email', checkout_session.get('customer_email', '')),
        stripe_customer_id=checkout_session['customer'],
        stripe_subscription_id=checkout_session.get('subscription'),
        subscription_status='trialing' if checkout_session.get('subscription') else 'active'
    )
    db.session.add(venue)
    db.session.commit()
    
    # Send welcome email
    try:
        send_welcome_email(venue)
    except Exception as e:
        app.logger.error(f"Failed to send welcome email: {e}")

def handle_subscription_updated(subscription):
    """Update venue subscription status"""
    venue = Venue.query.filter_by(stripe_subscription_id=subscription['id']).first()
    if venue:
        venue.subscription_status = subscription['status']
        db.session.commit()

def handle_subscription_deleted(subscription):
    """Handle subscription cancellation"""
    venue = Venue.query.filter_by(stripe_subscription_id=subscription['id']).first()
    if venue:
        venue.subscription_status = 'canceled'
        venue.active = False
        db.session.commit()

def handle_payment_failed(invoice):
    """Handle failed payment"""
    customer_id = invoice.get('customer')
    venue = Venue.query.filter_by(stripe_customer_id=customer_id).first()
    if venue:
        venue.subscription_status = 'past_due'
        db.session.commit()
        # Could send a "please update payment" email here

def send_welcome_email(venue):
    """Send welcome email to new venue"""
    qr_url = f"{BASE_URL}{venue.capture_url}"
    msg = Message(
        subject=f"🎉 Welcome to QR Lead Capture, {venue.name}!",
        recipients=[venue.email],
        html=render_template('email/welcome.html', venue=venue, qr_url=qr_url, base_url=BASE_URL)
    )
    mail.send(msg)

# =============================================================================
# PAYPAL CHECKOUT & BILLING
# =============================================================================

@app.route('/api/paypal/create-subscription', methods=['POST'])
def paypal_create_subscription():
    """Create PayPal subscription - called from client-side"""
    try:
        data = request.get_json() or {}
        venue_name = data.get('venue_name') or session.get('pending_venue_name', 'New Venue')
        email = data.get('email') or session.get('pending_email', '')
        subscription_id = data.get('subscription_id')
        
        app.logger.info(f"PayPal subscription request: venue={venue_name}, email={email}, sub_id={subscription_id}")
        
        if not subscription_id:
            return jsonify({'error': 'No subscription ID provided'}), 400
        
        # Verify subscription with PayPal
        try:
            access_token = get_paypal_access_token()
            if not access_token:
                app.logger.error("Failed to get PayPal access token")
                return jsonify({'error': 'Payment verification failed'}), 500
                
            response = requests.get(
                f"{PAYPAL_API_BASE}/v1/billing/subscriptions/{subscription_id}",
                headers={'Authorization': f'Bearer {access_token}'}
            )
            
            if response.status_code != 200:
                app.logger.error(f"PayPal verification failed: {response.status_code} - {response.text}")
                return jsonify({'error': 'Could not verify subscription with PayPal'}), 400
            
            sub_data = response.json()
            app.logger.info(f"PayPal subscription verified: status={sub_data.get('status')}")
            
        except requests.RequestException as e:
            app.logger.error(f"PayPal API error: {e}")
            return jsonify({'error': 'Payment service unavailable'}), 503
        
        # Check if venue already exists with this subscription
        existing = Venue.query.filter_by(paypal_subscription_id=subscription_id).first()
        if existing:
            session['venue_id'] = existing.id
            return jsonify({'success': True, 'venue_id': existing.id, 'redirect': url_for('dashboard')})
        
        # Also check by email to prevent duplicates
        email_to_use = (email or sub_data.get('subscriber', {}).get('email_address', '')).strip().lower()
        existing_email = Venue.query.filter(db.func.lower(Venue.email) == email_to_use).first()
        if existing_email:
            # Update existing venue with PayPal info
            existing_email.paypal_subscription_id = subscription_id
            existing_email.payment_provider = 'paypal'
            existing_email.subscription_status = 'trialing' if sub_data.get('status') in ('APPROVAL_PENDING', 'ACTIVE') else 'active'
            db.session.commit()
            session['venue_id'] = existing_email.id
            return jsonify({'success': True, 'venue_id': existing_email.id, 'redirect': url_for('dashboard')})
        
        # Create new venue
        venue = Venue(
            name=venue_name,
            email=email_to_use,
            paypal_subscription_id=subscription_id,
            subscription_status='trialing',  # PayPal plan has 7-day trial
            payment_provider='paypal'
        )
        db.session.add(venue)
        db.session.commit()
        
        app.logger.info(f"Created venue: id={venue.id}, slug={venue.slug}")
        
        # Log them in
        session['venue_id'] = venue.id
        
        # Clear pending data
        session.pop('pending_venue_name', None)
        session.pop('pending_email', None)
        
        # Send welcome email
        try:
            send_welcome_email(venue)
        except Exception as e:
            app.logger.error(f"Failed to send welcome email: {e}")
        
        return jsonify({'success': True, 'venue_id': venue.id, 'redirect': url_for('signup_success_paypal')})
        
    except Exception as e:
        app.logger.error(f"PayPal subscription error: {e}")
        db.session.rollback()
        return jsonify({'error': 'Account creation failed. Please try again or contact support.'}), 500

@app.route('/signup/success/paypal')
def signup_success_paypal():
    """PayPal post-subscription success page"""
    venue = get_current_venue()
    if not venue:
        return redirect(url_for('home'))
    return render_template('signup_success.html', venue=venue, base_url=BASE_URL)

@app.route('/webhook/paypal', methods=['POST'])
def paypal_webhook():
    """Handle PayPal webhooks"""
    # For production, verify webhook signature
    data = request.get_json()
    event_type = data.get('event_type')
    resource = data.get('resource', {})
    
    app.logger.info(f"PayPal webhook: {event_type}")
    
    if event_type == 'BILLING.SUBSCRIPTION.ACTIVATED':
        subscription_id = resource.get('id')
        venue = Venue.query.filter_by(paypal_subscription_id=subscription_id).first()
        if venue:
            venue.subscription_status = 'active'
            db.session.commit()
    
    elif event_type == 'BILLING.SUBSCRIPTION.CANCELLED':
        subscription_id = resource.get('id')
        venue = Venue.query.filter_by(paypal_subscription_id=subscription_id).first()
        if venue:
            venue.subscription_status = 'canceled'
            venue.active = False
            db.session.commit()
    
    elif event_type == 'BILLING.SUBSCRIPTION.SUSPENDED':
        subscription_id = resource.get('id')
        venue = Venue.query.filter_by(paypal_subscription_id=subscription_id).first()
        if venue:
            venue.subscription_status = 'past_due'
            db.session.commit()
    
    elif event_type == 'PAYMENT.SALE.COMPLETED':
        # Recurring payment successful
        pass
    
    return jsonify({'received': True})

# =============================================================================
# VENUE DASHBOARD
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Simple email-based login"""
    if request.method == 'GET':
        return render_template('login.html')
    
    try:
        email = request.form.get('email', '').strip().lower()
        
        if not email:
            flash('Please enter your email', 'error')
            return render_template('login.html')
        
        # Try exact match first, then case-insensitive
        venue = Venue.query.filter_by(email=email).first()
        if not venue:
            venue = Venue.query.filter(db.func.lower(Venue.email) == email).first()
        
        if venue:
            session['venue_id'] = venue.id
            return redirect(url_for('dashboard'))
        
        flash('No account found with that email. Please sign up first.', 'error')
        return render_template('login.html')
        
    except Exception as e:
        app.logger.error(f"Login error: {e}")
        flash('Something went wrong. Please try again.', 'error')
        return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('venue_id', None)
    return redirect(url_for('home'))

@app.route('/dashboard')
@venue_required
def dashboard():
    """Venue owner dashboard - see leads"""
    try:
        venue = get_current_venue()
        if not venue:
            session.pop('venue_id', None)
            flash('Session expired. Please log in again.', 'warning')
            return redirect(url_for('login'))
        
        leads = venue.leads.order_by(Lead.created_at.desc()).limit(100).all()
        
        return render_template('dashboard.html', 
                             venue=venue, 
                             leads=leads,
                             base_url=BASE_URL)
    except Exception as e:
        app.logger.error(f"Dashboard error: {e}")
        flash('Something went wrong loading your dashboard.', 'error')
        return redirect(url_for('home'))

@app.route('/dashboard/settings', methods=['GET', 'POST'])
@venue_required
def settings():
    """Venue settings"""
    venue = get_current_venue()
    
    if request.method == 'POST':
        venue.name = request.form.get('name', venue.name)
        venue.welcome_message = request.form.get('welcome_message', venue.welcome_message)
        venue.thank_you_message = request.form.get('thank_you_message', venue.thank_you_message)
        venue.primary_color = request.form.get('primary_color', venue.primary_color)
        db.session.commit()
        flash('Settings saved!', 'success')
    
    return render_template('settings.html', venue=venue)

@app.route('/dashboard/billing')
@venue_required
def billing():
    """Redirect to Stripe billing portal"""
    venue = get_current_venue()
    
    if not venue.stripe_customer_id:
        flash('No billing information found', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        portal = stripe.billing_portal.Session.create(
            customer=venue.stripe_customer_id,
            return_url=f"{BASE_URL}/dashboard"
        )
        return redirect(portal.url)
    except Exception as e:
        app.logger.error(f"Stripe portal error: {e}")
        flash('Could not open billing portal', 'error')
        return redirect(url_for('dashboard'))

@app.route('/dashboard/leads/export')
@venue_required
def export_leads():
    """Export leads as CSV"""
    venue = get_current_venue()
    leads = venue.leads.order_by(Lead.created_at.desc()).all()
    
    import csv
    from io import StringIO
    from flask import Response
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Phone', 'Date'])
    
    for lead in leads:
        writer.writerow([
            lead.name or '',
            lead.email or '',
            lead.phone or '',
            lead.created_at.strftime('%Y-%m-%d %H:%M') if lead.created_at else ''
        ])
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={venue.slug}_leads.csv'}
    )

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route('/api/leads', methods=['GET'])
@venue_required
def api_leads():
    """API endpoint to get leads"""
    venue = get_current_venue()
    leads = venue.leads.order_by(Lead.created_at.desc()).limit(100).all()
    return jsonify([lead.to_dict() for lead in leads])

@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        venue_count = Venue.query.count()
        lead_count = Lead.query.count()
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'venues': venue_count,
            'leads': lead_count,
            'stripe_enabled': STRIPE_ENABLED,
            'paypal_enabled': PAYPAL_ENABLED
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.route('/api/debug/venue/<email>')
def debug_venue(email):
    """Debug endpoint to check if venue exists (remove in production)"""
    try:
        venue = Venue.query.filter(db.func.lower(Venue.email) == email.lower()).first()
        if venue:
            return jsonify({
                'found': True,
                'id': venue.id,
                'name': venue.name,
                'email': venue.email,
                'slug': venue.slug,
                'status': venue.subscription_status,
                'provider': venue.payment_provider,
                'created': venue.created_at.isoformat() if venue.created_at else None
            })
        return jsonify({'found': False, 'email_searched': email})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# INIT
# =============================================================================

@app.cli.command('init-db')
def init_db():
    """Initialize the database"""
    db.create_all()
    print('Database initialized!')

@app.before_request
def ensure_db():
    """Create tables if they don't exist"""
    # This runs on first request only
    if not hasattr(app, '_db_initialized'):
        db.create_all()
        app._db_initialized = True

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
