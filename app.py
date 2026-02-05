# app.py - QR Lead Capture SaaS
# Dual payment processing: Stripe + PayPal for redundancy

import os
import stripe
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, Response, send_file
from flask_mail import Mail, Message
from functools import wraps
from datetime import datetime
from io import BytesIO

# Menu upload config
ALLOWED_MENU_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'webp'}
MAX_MENU_SIZE = 10 * 1024 * 1024  # 10MB

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
    
    # Select template (default to modern if not set)
    template_name = venue.template or 'modern'
    template_file = f'capture/{template_name}.html'
    
    # Fallback to default if template doesn't exist
    try:
        return render_template(template_file, venue=venue, base_url=BASE_URL)
    except:
        return render_template('capture/modern.html', venue=venue, base_url=BASE_URL)

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

@app.route('/api/dashboard/stats')
@venue_required
def dashboard_stats():
    """API endpoint for real-time dashboard updates"""
    venue = get_current_venue()
    if not venue:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Get recent leads (last 50)
    leads = venue.leads.order_by(Lead.created_at.desc()).limit(50).all()
    
    return jsonify({
        'stats': {
            'total': venue.lead_count,
            'this_month': venue.leads_this_month,
            'this_week': venue.leads_this_week,
            'today': venue.leads_today
        },
        'leads': [lead.to_dict() for lead in leads],
        'updated_at': datetime.utcnow().isoformat()
    })

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
        
        # Template settings
        venue.template = request.form.get('template', venue.template or 'modern')
        venue.tagline = request.form.get('tagline', '').strip() or None
        venue.incentive = request.form.get('incentive', '').strip() or None
        venue.show_social_proof = request.form.get('show_social_proof') == 'on'
        
        db.session.commit()
        flash('Settings saved!', 'success')
    
    # Available templates
    templates = [
        {'id': 'modern', 'name': 'Modern', 'desc': 'Clean & minimal', 'preview': 'bg-gradient-to-br from-slate-900 to-slate-800'},
        {'id': 'elegant', 'name': 'Elegant', 'desc': 'Upscale & sophisticated', 'preview': 'bg-gradient-to-br from-stone-900 to-amber-950'},
        {'id': 'vibrant', 'name': 'Vibrant', 'desc': 'Bold & energetic', 'preview': 'bg-gradient-to-br from-purple-600 to-pink-500'},
        {'id': 'cozy', 'name': 'Cozy', 'desc': 'Warm & inviting', 'preview': 'bg-gradient-to-br from-orange-800 to-amber-700'},
        {'id': 'minimal', 'name': 'Minimal', 'desc': 'Simple & light', 'preview': 'bg-gradient-to-br from-gray-100 to-white'},
    ]
    
    return render_template('settings.html', venue=venue, base_url=BASE_URL, templates=templates)

# =============================================================================
# MENU UPLOAD & SERVING
# =============================================================================

# Extended allowed extensions (including HEIC which we'll convert)
ALLOWED_MENU_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif'}

def allowed_menu_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_MENU_EXTENSIONS

def detect_image_format(file_data):
    """Detect actual image format from magic bytes"""
    if len(file_data) < 12:
        return None
    
    # Check magic bytes
    if file_data[:4] == b'\x89PNG':
        return 'png'
    if file_data[:2] == b'\xff\xd8':
        return 'jpeg'
    if file_data[:4] == b'RIFF' and file_data[8:12] == b'WEBP':
        return 'webp'
    if file_data[:4] == b'%PDF':
        return 'pdf'
    # HEIC/HEIF detection (ftyp box with heic, mif1, etc.)
    if file_data[4:8] == b'ftyp':
        ftyp = file_data[8:12].decode('ascii', errors='ignore').lower()
        if 'heic' in ftyp or 'heix' in ftyp or 'hevc' in ftyp or 'mif1' in ftyp:
            return 'heic'
    return None

def convert_heic_to_jpeg(file_data):
    """Convert HEIC image to JPEG bytes"""
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
        from io import BytesIO
        
        # Register HEIF opener with Pillow
        register_heif_opener()
        
        # Open HEIC and convert to JPEG
        img = Image.open(BytesIO(file_data))
        
        # Convert to RGB if necessary (HEIC might have alpha channel)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        
        # Save as JPEG
        output = BytesIO()
        img.save(output, format='JPEG', quality=90)
        return output.getvalue()
    except Exception as e:
        app.logger.error(f"HEIC conversion error: {e}")
        return None

@app.route('/dashboard/menu/upload', methods=['POST'])
@venue_required
def upload_menu():
    """Handle menu file upload"""
    venue = get_current_venue()
    
    if 'menu' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('settings'))
    
    file = request.files['menu']
    
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('settings'))
    
    if not allowed_menu_file(file.filename):
        flash('Invalid file type. Please upload PDF, PNG, JPG, WEBP, or HEIC.', 'error')
        return redirect(url_for('settings'))
    
    # Read file data
    file_data = file.read()
    
    # Check file size
    if len(file_data) > MAX_MENU_SIZE:
        flash(f'File too large. Maximum size is {MAX_MENU_SIZE // (1024*1024)}MB.', 'error')
        return redirect(url_for('settings'))
    
    # Detect actual format (not just by extension)
    actual_format = detect_image_format(file_data)
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    
    # Handle HEIC conversion (common iPhone format)
    if actual_format == 'heic' or ext in ('heic', 'heif'):
        converted = convert_heic_to_jpeg(file_data)
        if converted:
            file_data = converted
            actual_format = 'jpeg'
            filename = file.filename.rsplit('.', 1)[0] + '.jpg'
            flash('Menu uploaded! (Converted from HEIC to JPEG)', 'success')
        else:
            flash('Could not convert HEIC image. Please convert to JPG/PNG first.', 'error')
            return redirect(url_for('settings'))
    else:
        filename = file.filename
    
    # Determine content type based on actual format
    content_types = {
        'pdf': 'application/pdf',
        'png': 'image/png',
        'jpeg': 'image/jpeg',
        'jpg': 'image/jpeg',
        'webp': 'image/webp'
    }
    
    # Use detected format if available, otherwise fall back to extension
    content_type = content_types.get(actual_format) or content_types.get(ext, 'application/octet-stream')
    
    # Save to database
    venue.menu_data = file_data
    venue.menu_filename = filename
    venue.menu_content_type = content_type
    db.session.commit()
    
    # Flash success if not already done (HEIC conversion already flashed)
    if actual_format != 'heic' and ext not in ('heic', 'heif'):
        flash('Menu uploaded successfully!', 'success')
    return redirect(url_for('settings'))

@app.route('/dashboard/menu/delete', methods=['POST'])
@venue_required
def delete_menu():
    """Delete uploaded menu"""
    venue = get_current_venue()
    
    venue.menu_data = None
    venue.menu_filename = None
    venue.menu_content_type = None
    db.session.commit()
    
    flash('Menu deleted.', 'success')
    return redirect(url_for('settings'))

@app.route('/menu/<slug>')
def serve_menu(slug):
    """Serve menu file for a venue"""
    venue = Venue.query.filter_by(slug=slug).first_or_404()
    
    if not venue.has_menu:
        return "No menu available", 404
    
    # Check subscription is active (optional - could allow menu viewing even if expired)
    # if venue.subscription_status not in ('active', 'trialing'):
    #     return "Menu unavailable", 402
    
    return Response(
        venue.menu_data,
        mimetype=venue.menu_content_type,
        headers={
            'Content-Disposition': f'inline; filename="{venue.menu_filename}"',
            'Cache-Control': 'public, max-age=3600'  # Cache for 1 hour
        }
    )

# =============================================================================
# LOGO UPLOAD & SERVING
# =============================================================================

ALLOWED_LOGO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'svg', 'heic', 'heif'}
MAX_LOGO_SIZE = 2 * 1024 * 1024  # 2MB

def allowed_logo_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_LOGO_EXTENSIONS

@app.route('/dashboard/logo/upload', methods=['POST'])
@venue_required
def upload_logo():
    """Handle logo file upload"""
    venue = get_current_venue()
    
    if 'logo' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('settings'))
    
    file = request.files['logo']
    
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('settings'))
    
    if not allowed_logo_file(file.filename):
        flash('Invalid file type. Please upload PNG, JPG, WEBP, SVG, or HEIC.', 'error')
        return redirect(url_for('settings'))
    
    file_data = file.read()
    
    if len(file_data) > MAX_LOGO_SIZE:
        flash(f'File too large. Maximum size is {MAX_LOGO_SIZE // (1024*1024)}MB.', 'error')
        return redirect(url_for('settings'))
    
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    
    # Detect actual format and handle HEIC
    actual_format = detect_image_format(file_data)
    if actual_format == 'heic' or ext in ('heic', 'heif'):
        converted = convert_heic_to_jpeg(file_data)
        if converted:
            file_data = converted
            actual_format = 'jpeg'
            filename = file.filename.rsplit('.', 1)[0] + '.jpg'
        else:
            flash('Could not convert HEIC image. Please convert to JPG/PNG first.', 'error')
            return redirect(url_for('settings'))
    else:
        filename = file.filename
    
    content_types = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
        'svg': 'image/svg+xml'
    }
    
    # Use detected format for content type if available
    content_type = content_types.get(actual_format) or content_types.get(ext, 'image/png')
    
    venue.logo_data = file_data
    venue.logo_filename = filename
    venue.logo_content_type = content_type
    db.session.commit()
    
    flash('Logo uploaded successfully!', 'success')
    return redirect(url_for('settings'))

@app.route('/dashboard/logo/delete', methods=['POST'])
@venue_required
def delete_logo():
    """Delete uploaded logo"""
    venue = get_current_venue()
    venue.logo_data = None
    venue.logo_filename = None
    venue.logo_content_type = None
    db.session.commit()
    flash('Logo deleted.', 'success')
    return redirect(url_for('settings'))

@app.route('/logo/<slug>')
def serve_logo(slug):
    """Serve logo for a venue"""
    venue = Venue.query.filter_by(slug=slug).first_or_404()
    
    if not venue.has_logo:
        return "No logo available", 404
    
    return Response(
        venue.logo_data,
        mimetype=venue.logo_content_type,
        headers={
            'Content-Disposition': f'inline; filename="{venue.logo_filename}"',
            'Cache-Control': 'public, max-age=86400'  # Cache for 24 hours
        }
    )

@app.route('/dashboard/menu/fix-heic', methods=['POST'])
@venue_required
def fix_heic_menu():
    """Convert existing HEIC menu to JPEG"""
    venue = get_current_venue()
    
    if not venue.has_menu:
        flash('No menu to fix.', 'error')
        return redirect(url_for('settings'))
    
    # Check if it's actually HEIC
    actual_format = detect_image_format(venue.menu_data)
    if actual_format != 'heic':
        flash('Menu is not HEIC format, no conversion needed.', 'info')
        return redirect(url_for('settings'))
    
    # Convert
    converted = convert_heic_to_jpeg(venue.menu_data)
    if converted:
        venue.menu_data = converted
        venue.menu_filename = venue.menu_filename.rsplit('.', 1)[0] + '.jpg' if venue.menu_filename else 'menu.jpg'
        venue.menu_content_type = 'image/jpeg'
        db.session.commit()
        flash('Menu converted from HEIC to JPEG!', 'success')
    else:
        flash('Could not convert HEIC. Please re-upload as JPG.', 'error')
    
    return redirect(url_for('settings'))

@app.route('/dashboard/billing')
@venue_required
def billing():
    """Redirect to appropriate billing portal based on payment provider"""
    venue = get_current_venue()
    paypal_manage_url = "https://www.paypal.com/myaccount/autopay/"
    
    # Check payment provider - PayPal first (most common)
    if venue.payment_provider == 'paypal' or venue.paypal_subscription_id:
        flash('Manage your subscription on PayPal.', 'info')
        return redirect(paypal_manage_url)
    
    # Stripe users
    if venue.stripe_customer_id:
        try:
            portal = stripe.billing_portal.Session.create(
                customer=venue.stripe_customer_id,
                return_url=f"{BASE_URL}/dashboard"
            )
            return redirect(portal.url)
        except Exception as e:
            app.logger.error(f"Stripe portal error: {e}")
            flash('Could not open billing portal. Please try again later.', 'error')
            return redirect(url_for('dashboard'))
    
    # Manual/unknown provider - default to PayPal since it's primary
    # This handles venues created via admin or incomplete signup
    if venue.payment_provider == 'manual' or venue.subscription_status == 'trialing':
        flash('Manage your subscription on PayPal.', 'info')
        return redirect(paypal_manage_url)
    
    # Fallback error
    flash('No billing information found. Please contact support.', 'error')
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
                'provider': getattr(venue, 'payment_provider', 'unknown'),
                'created': venue.created_at.isoformat() if venue.created_at else None
            })
        return jsonify({'found': False, 'email_searched': email})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/migrate')
def run_db_migration():
    """Manually trigger database migration"""
    try:
        run_migrations()
        return jsonify({'status': 'migrations_complete'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/paypal')
def debug_paypal():
    """Debug PayPal connection"""
    try:
        access_token = get_paypal_access_token()
        if not access_token:
            return jsonify({'status': 'error', 'message': 'Failed to get access token'})
        return jsonify({
            'status': 'ok',
            'access_token_prefix': access_token[:20] + '...',
            'api_base': PAYPAL_API_BASE,
            'plan_id': PAYPAL_PLAN_ID,
            'client_id_prefix': PAYPAL_CLIENT_ID[:20] + '...' if PAYPAL_CLIENT_ID else None
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/debug/paypal/subscription/<sub_id>')
def debug_paypal_subscription(sub_id):
    """Debug a specific PayPal subscription"""
    try:
        access_token = get_paypal_access_token()
        if not access_token:
            return jsonify({'error': 'Failed to get PayPal access token'}), 500
        
        response = requests.get(
            f"{PAYPAL_API_BASE}/v1/billing/subscriptions/{sub_id}",
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        return jsonify({
            'status_code': response.status_code,
            'response': response.json() if response.status_code == 200 else response.text
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/create-venue', methods=['POST'])
def admin_create_venue():
    """Admin endpoint to manually create a venue (for debugging)"""
    # Simple auth check - require secret key
    auth = request.headers.get('X-Admin-Key')
    if auth != app.secret_key:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json() or {}
        name = data.get('name', 'New Venue')
        email = data.get('email', '').strip().lower()
        
        if not email:
            return jsonify({'error': 'Email required'}), 400
        
        # Check if exists
        existing = Venue.query.filter(db.func.lower(Venue.email) == email).first()
        if existing:
            return jsonify({
                'status': 'exists',
                'venue_id': existing.id,
                'slug': existing.slug
            })
        
        # Create venue
        venue = Venue(
            name=name,
            email=email,
            subscription_status='trialing',
            payment_provider='manual'
        )
        db.session.add(venue)
        db.session.commit()
        
        return jsonify({
            'status': 'created',
            'venue_id': venue.id,
            'slug': venue.slug,
            'email': venue.email
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/update-venue-payment', methods=['POST'])
def admin_update_venue_payment():
    """Admin endpoint to update venue payment provider"""
    auth = request.headers.get('X-Admin-Key')
    if auth != app.secret_key:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json() or {}
        email = data.get('email', '').strip().lower()
        payment_provider = data.get('payment_provider', 'paypal')
        paypal_subscription_id = data.get('paypal_subscription_id')
        
        if not email:
            return jsonify({'error': 'Email required'}), 400
        
        venue = Venue.query.filter(db.func.lower(Venue.email) == email).first()
        if not venue:
            return jsonify({'error': 'Venue not found'}), 404
        
        venue.payment_provider = payment_provider
        if paypal_subscription_id:
            venue.paypal_subscription_id = paypal_subscription_id
        db.session.commit()
        
        return jsonify({
            'status': 'updated',
            'venue_id': venue.id,
            'email': venue.email,
            'payment_provider': venue.payment_provider,
            'paypal_subscription_id': venue.paypal_subscription_id
        })
    except Exception as e:
        db.session.rollback()
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
    """Create tables and run migrations if needed"""
    # This runs on first request only
    if not hasattr(app, '_db_initialized'):
        db.create_all()
        # Run migrations for missing columns
        try:
            run_migrations()
        except Exception as e:
            app.logger.error(f"Migration error: {e}")
        app._db_initialized = True

def run_migrations():
    """Add any missing columns to existing tables"""
    from sqlalchemy import text
    
    migrations = [
        # Add payment_provider column if missing
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='payment_provider') THEN
                ALTER TABLE venues ADD COLUMN payment_provider VARCHAR(20) DEFAULT 'stripe';
            END IF;
        END $$;
        """,
        # Add paypal_subscription_id if missing
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='paypal_subscription_id') THEN
                ALTER TABLE venues ADD COLUMN paypal_subscription_id VARCHAR(100);
            END IF;
        END $$;
        """,
        # Add menu_data column (BYTEA for binary storage)
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='menu_data') THEN
                ALTER TABLE venues ADD COLUMN menu_data BYTEA;
            END IF;
        END $$;
        """,
        # Add menu_filename column
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='menu_filename') THEN
                ALTER TABLE venues ADD COLUMN menu_filename VARCHAR(255);
            END IF;
        END $$;
        """,
        # Add menu_content_type column
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='menu_content_type') THEN
                ALTER TABLE venues ADD COLUMN menu_content_type VARCHAR(100);
            END IF;
        END $$;
        """,
        # Template system columns
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='template') THEN
                ALTER TABLE venues ADD COLUMN template VARCHAR(20) DEFAULT 'modern';
            END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='tagline') THEN
                ALTER TABLE venues ADD COLUMN tagline VARCHAR(200);
            END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='incentive') THEN
                ALTER TABLE venues ADD COLUMN incentive VARCHAR(200);
            END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='show_social_proof') THEN
                ALTER TABLE venues ADD COLUMN show_social_proof BOOLEAN DEFAULT TRUE;
            END IF;
        END $$;
        """,
        # Logo storage columns
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='logo_data') THEN
                ALTER TABLE venues ADD COLUMN logo_data BYTEA;
            END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='logo_filename') THEN
                ALTER TABLE venues ADD COLUMN logo_filename VARCHAR(255);
            END IF;
        END $$;
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='venues' AND column_name='logo_content_type') THEN
                ALTER TABLE venues ADD COLUMN logo_content_type VARCHAR(100);
            END IF;
        END $$;
        """,
    ]
    
    for migration in migrations:
        try:
            db.session.execute(text(migration))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"Migration skipped or failed: {e}")

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
