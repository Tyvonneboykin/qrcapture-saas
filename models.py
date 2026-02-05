# models.py - Database Models for QR Lead Capture SaaS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import secrets
import string

db = SQLAlchemy()

def generate_venue_slug(length=8):
    """Generate a short, URL-safe venue identifier"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

class Venue(db.Model):
    """A business/venue that uses the lead capture system"""
    __tablename__ = 'venues'
    
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(16), unique=True, nullable=False, default=generate_venue_slug)
    
    # Business info
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)  # Where leads get sent
    phone = db.Column(db.String(50))
    
    # Customization
    welcome_message = db.Column(db.Text, default="Welcome! Enter your info for exclusive offers.")
    thank_you_message = db.Column(db.Text, default="Thanks! We'll be in touch soon.")
    logo_url = db.Column(db.String(500))
    primary_color = db.Column(db.String(7), default="#6366f1")  # Hex color
    
    # Template system
    template = db.Column(db.String(20), default='modern')  # modern, elegant, vibrant, cozy, minimal
    tagline = db.Column(db.String(200))  # Subtitle under venue name
    incentive = db.Column(db.String(200))  # Specific offer: "Get 10% off your first visit!"
    show_social_proof = db.Column(db.Boolean, default=True)  # Show "Join X others"
    
    # Logo storage (stored in DB like menu)
    logo_data = db.Column(db.LargeBinary)
    logo_filename = db.Column(db.String(255))
    logo_content_type = db.Column(db.String(100))
    
    # Menu storage (stored in DB for simplicity - migrate to R2 at scale)
    menu_data = db.Column(db.LargeBinary)  # Binary file data
    menu_filename = db.Column(db.String(255))  # Original filename
    menu_content_type = db.Column(db.String(100))  # MIME type (application/pdf, image/png, etc.)
    
    # Billing (Stripe or PayPal)
    payment_provider = db.Column(db.String(20), default='stripe')  # stripe or paypal
    stripe_customer_id = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    paypal_subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(50), default='trialing')  # trialing, active, past_due, canceled
    
    # Status
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    leads = db.relationship('Lead', backref='venue', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Venue {self.name}>'
    
    @property
    def capture_url(self):
        """The URL customers scan to enter their info"""
        return f"/c/{self.slug}"
    
    @property
    def lead_count(self):
        return self.leads.count()
    
    @property
    def leads_this_month(self):
        from datetime import datetime
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.leads.filter(Lead.created_at >= start_of_month).count()
    
    @property
    def leads_this_week(self):
        from datetime import datetime, timedelta
        start_of_week = datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.leads.filter(Lead.created_at >= start_of_week).count()
    
    @property
    def leads_today(self):
        from datetime import datetime
        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.leads.filter(Lead.created_at >= start_of_day).count()
    
    @property
    def has_logo(self):
        """Check if venue has a logo uploaded"""
        return self.logo_data is not None and len(self.logo_data) > 0
    
    @property
    def has_menu(self):
        """Check if venue has a menu uploaded"""
        return self.menu_data is not None and len(self.menu_data) > 0
    
    @property
    def menu_url(self):
        """URL to view the menu"""
        return f"/menu/{self.slug}" if self.has_menu else None
    
    @property 
    def logo_url_path(self):
        """URL to serve the logo"""
        return f"/logo/{self.slug}" if self.has_logo else None


class Lead(db.Model):
    """A captured customer lead"""
    __tablename__ = 'leads'
    
    id = db.Column(db.Integer, primary_key=True)
    venue_id = db.Column(db.Integer, db.ForeignKey('venues.id'), nullable=False)
    
    # Contact info (at least one required)
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    name = db.Column(db.String(200))
    
    # Metadata
    source = db.Column(db.String(50), default='qr')  # qr, web, manual
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Lead {self.email or self.phone}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'phone': self.phone,
            'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
