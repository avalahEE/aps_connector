# -*- coding: utf-8 -*-
"""
APS Connector API Controller

This module transforms Odoo data to APS format. ALL transformations happen here.
The APS backend receives clean, ready-to-store JSON - no transformation needed on that side.

Format conventions:
- All field names are camelCase (matching APS TypeScript entities)
- All IDs are strings (externalId pattern)
- All dates are ISO 8601 format
- All enums use APS values (RELEASED, not "confirmed")
- Efficiency is decimal (0.95, not 95%)

Security:
- API keys are stored as SHA-256 hashes
- Rate limiting protects against brute force attacks
- All access is logged for audit purposes
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from odoo import http, fields, SUPERUSER_ID
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

API_VERSION = '3'  # Bumped for security improvements


# =============================================================================
# RATE LIMITING - Protects against brute force attacks
# =============================================================================
_rate_limit_lock = threading.Lock()
_failed_attempts_by_ip = {}  # ip -> (count, first_attempt_time)
_failed_attempts_by_key = {}  # api_key_prefix -> (count, first_attempt_time)
RATE_LIMIT_WINDOW = 60  # seconds
MAX_FAILED_ATTEMPTS = 10

# Expanding a template BOM across its variants is bounded — configurable
# products can have thousands and APS only needs the ones that get produced
MAX_BOM_VARIANTS = 20

# Set to True only if running behind a trusted reverse proxy (nginx, load balancer)
# When False, X-Forwarded-For header is ignored to prevent spoofing
TRUST_X_FORWARDED_FOR = False


def _get_client_ip():
    """Get client IP address from request.

    Security note: Only trusts X-Forwarded-For if TRUST_X_FORWARDED_FOR is True.
    This prevents IP spoofing attacks that bypass rate limiting.
    """
    if TRUST_X_FORWARDED_FOR:
        forwarded = request.httprequest.headers.get('X-Forwarded-For')
        if forwarded:
            return forwarded.split(',')[0].strip()
    ip = request.httprequest.remote_addr
    if not ip:
        _logger.warning('Could not determine client IP address')
        ip = 'unresolved'
    return ip


def _get_key_prefix(api_key):
    """Get first 8 chars of API key for rate limiting (without revealing full key)"""
    if api_key and len(api_key) >= 8:
        return api_key[:8]
    _logger.debug('API key too short or missing for prefix extraction')
    return 'short-key'


def _check_rate_limit(ip_address, api_key=None):
    """Check if IP or API key prefix has exceeded rate limit. Returns True if allowed."""
    with _rate_limit_lock:
        now = datetime.utcnow()

        # Check IP-based rate limit
        if ip_address in _failed_attempts_by_ip:
            count, first_time = _failed_attempts_by_ip[ip_address]
            if (now - first_time).total_seconds() > RATE_LIMIT_WINDOW:
                del _failed_attempts_by_ip[ip_address]
            elif count >= MAX_FAILED_ATTEMPTS:
                return False

        # Check API key prefix rate limit (additional protection)
        if api_key:
            key_prefix = _get_key_prefix(api_key)
            if key_prefix in _failed_attempts_by_key:
                count, first_time = _failed_attempts_by_key[key_prefix]
                if (now - first_time).total_seconds() > RATE_LIMIT_WINDOW:
                    del _failed_attempts_by_key[key_prefix]
                elif count >= MAX_FAILED_ATTEMPTS:
                    return False

        return True


def _record_failed_attempt(ip_address, api_key=None):
    """Record a failed authentication attempt for both IP and API key prefix."""
    with _rate_limit_lock:
        now = datetime.utcnow()

        # Record by IP
        if ip_address in _failed_attempts_by_ip:
            count, first_time = _failed_attempts_by_ip[ip_address]
            _failed_attempts_by_ip[ip_address] = (count + 1, first_time)
        else:
            _failed_attempts_by_ip[ip_address] = (1, now)

        # Record by API key prefix (if provided)
        if api_key:
            key_prefix = _get_key_prefix(api_key)
            if key_prefix in _failed_attempts_by_key:
                count, first_time = _failed_attempts_by_key[key_prefix]
                _failed_attempts_by_key[key_prefix] = (count + 1, first_time)
            else:
                _failed_attempts_by_key[key_prefix] = (1, now)


def _clear_failed_attempts(ip_address, api_key=None):
    """Clear failed attempts after successful auth."""
    with _rate_limit_lock:
        if ip_address in _failed_attempts_by_ip:
            del _failed_attempts_by_ip[ip_address]
        if api_key:
            key_prefix = _get_key_prefix(api_key)
            if key_prefix in _failed_attempts_by_key:
                del _failed_attempts_by_key[key_prefix]


# =============================================================================
# AUDIT LOGGING
# =============================================================================
def _log_api_access(endpoint, config, success, error=None):
    """Log API access for audit purposes"""
    ip_address = _get_client_ip()
    if success:
        _logger.info(
            'APS API access: endpoint=%s, config=%s, ip=%s, success=True',
            endpoint, config.name if config else 'NONE', ip_address
        )
    else:
        _logger.warning(
            'APS API access FAILED: endpoint=%s, config=%s, ip=%s, error=%s',
            endpoint, config.name if config else 'NONE', ip_address, error or 'Unknown'
        )


# =============================================================================
# API KEY VALIDATION - Uses secure hash comparison
# =============================================================================
def json_response(data, status=200):
    """Return JSON response with proper headers"""
    return Response(
        json.dumps(data, default=str),
        status=status,
        headers={'Content-Type': 'application/json'},
    )


def validate_api_key(api_key, endpoint='unknown'):
    """
    Validate API key using constant-time hash comparison.

    Returns config if valid, None otherwise.
    Includes rate limiting (by IP and API key prefix) and audit logging.
    """
    ip_address = _get_client_ip()

    # Check rate limit first (both IP and API key prefix)
    if not _check_rate_limit(ip_address, api_key):
        _logger.warning('Rate limit exceeded for IP: %s on endpoint: %s', ip_address, endpoint)
        _log_api_access(endpoint, None, False, 'Rate limit exceeded')
        return None

    if not api_key:
        _record_failed_attempt(ip_address)
        _log_api_access(endpoint, None, False, 'No API key provided')
        return None

    # Get all active configs and verify using constant-time comparison
    configs = request.env['aps.sync.config'].sudo().search([
        ('active', '=', True),
        ('api_key_hash', '!=', False),
    ])

    for config in configs:
        if config.verify_api_key(api_key):
            _clear_failed_attempts(ip_address, api_key)
            _log_api_access(endpoint, config, True)
            return config

    # No matching config found - record failure for both IP and API key prefix
    _record_failed_attempt(ip_address, api_key)
    _log_api_access(endpoint, None, False, 'Invalid API key')
    return None


# =============================================================================
# MAPPING HELPERS - Odoo to APS transformations
# =============================================================================

def map_mo_state_to_aps(state):
    """Map Odoo MO state to APS OrderStatus"""
    mapping = {
        'draft': 'SUGGESTED',
        'confirmed': 'RELEASED',
        'progress': 'STARTED',
        'to_close': 'STARTED',
        'done': 'COMPLETE',
        'cancel': 'SUGGESTED',
    }
    return mapping.get(state, 'SUGGESTED')


def map_wo_state_to_aps(state):
    """Map Odoo WO state to APS OperationStatus"""
    mapping = {
        'pending': 'PENDING',
        'waiting': 'PENDING',
        'ready': 'SCHEDULED',
        'progress': 'IN_PROGRESS',
        'done': 'COMPLETE',
        'cancel': 'CANCELLED',
    }
    return mapping.get(state, 'PENDING')


def map_priority_to_aps(priority):
    """Map Odoo priority (0-3) to APS priority (100-750)"""
    mapping = {
        '0': 750,  # Normal → Low
        '1': 500,  # Low → Medium
        '2': 250,  # High → Medium-High
        '3': 100,  # Urgent → High
    }
    return mapping.get(str(priority), 500)


def map_product_type_to_aps(product_type):
    """Map Odoo product type to APS ProductType"""
    mapping = {
        'product': 'MANUFACTURED',
        'consu': 'PURCHASED',
        'service': 'PURCHASED',
    }
    return mapping.get(product_type, 'MANUFACTURED')


def map_bom_type_to_aps(bom_type):
    """Map Odoo BOM type to APS ProductType for the product"""
    if bom_type == 'phantom':
        return 'PHANTOM'
    return 'MANUFACTURED'


def format_time(decimal_hours):
    """Format decimal hours (e.g., 8.5) to HH:MM string"""
    hours = int(decimal_hours)
    minutes = int((decimal_hours - hours) * 60)
    return f"{hours:02d}:{minutes:02d}"


def build_week_pattern(attendances):
    """Build APS weekPattern from Odoo calendar attendances"""
    day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    pattern = {day: {'shifts': []} for day in day_names}

    for att in attendances:
        day_index = int(att.dayofweek)
        if 0 <= day_index < 7:
            day_name = day_names[day_index]
            pattern[day_name]['shifts'].append({
                'startTime': format_time(att.hour_from),
                'endTime': format_time(att.hour_to),
                'stateId': '',  # Will be set by APS
            })

    # Sort shifts by start time
    for day in day_names:
        pattern[day]['shifts'].sort(key=lambda s: s['startTime'])

    return pattern


class ApsApiController(http.Controller):
    """APS API Controller - Returns data in APS-ready format"""

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    @http.route('/aps/api/v1/health', type='json', auth='none', methods=['POST'], csrf=False)
    def health_check(self, **kwargs):
        """Health check endpoint"""
        try:
            api_key = kwargs.get('api_key')
            if not validate_api_key(api_key, '/health'):
                return {'error': 'Invalid or missing API key'}

            import odoo.release
            module = request.env['ir.module.module'].sudo().search(
                [('name', '=', 'aps_connector')], limit=1
            )
            return {
                'status': 'ok',
                'odooVersion': odoo.release.version,
                'moduleVersion': module.installed_version or 'unknown',
                'apiVersion': API_VERSION,
                'timestamp': datetime.utcnow().isoformat() + 'Z',
            }
        except Exception as e:
            _logger.exception('Health check failed')
            return {'error': 'Internal error'}

    # =========================================================================
    # CALENDAR EXCEPTIONS (Leaves)
    # =========================================================================

    @http.route('/aps/api/v1/calendar_exceptions', type='json', auth='none', methods=['POST'], csrf=False)
    def get_calendar_exceptions(self, **kwargs):
        """
        Get calendar exceptions (leaves/holidays) in APS format.
        Uses raw SQL for performance.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/calendar_exceptions')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            calendar_ids = kwargs.get('calendarExternalIds')
            date_from = kwargs.get('dateFrom')
            date_to = kwargs.get('dateTo')

            cr = request.env.cr
            where_clauses = [
                "rcl.calendar_id IN (SELECT id FROM resource_calendar WHERE company_id = %s OR company_id IS NULL)"
            ]
            params = [company_id]

            if calendar_ids:
                where_clauses.append("rcl.calendar_id = ANY(%s)")
                params.append([int(cid) for cid in calendar_ids])
            if date_from:
                where_clauses.append("rcl.date_to >= %s")
                params.append(date_from)
            if date_to:
                where_clauses.append("rcl.date_from <= %s")
                params.append(date_to)

            where_sql = " WHERE " + " AND ".join(where_clauses)

            cr.execute(f"""
                SELECT rcl.id, rcl.calendar_id, rcl.name, rcl.date_from, rcl.date_to
                FROM resource_calendar_leaves rcl
                {where_sql}
                ORDER BY rcl.id
            """, params)
            rows = cr.dictfetchall()

            records = []
            for r in rows:
                records.append({
                    'externalId': str(r['id']),
                    'calendarExternalId': str(r['calendar_id']) if r['calendar_id'] else None,
                    'name': r['name'],
                    'startDate': r['date_from'].isoformat() + 'Z' if r['date_from'] else None,
                    'endDate': r['date_to'].isoformat() + 'Z' if r['date_to'] else None,
                    'exceptionType': 'NON_WORKING',
                })

            return {
                'success': True,
                'total': len(records),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching calendar exceptions')
            return {'error': 'Internal error'}

    # =========================================================================
    # MAINTENANCE REQUESTS (blocking work center time)
    # =========================================================================

    @http.route('/aps/api/v1/maintenance_requests', type='json', auth='none', methods=['POST'], csrf=False)
    def get_maintenance_requests(self, **kwargs):
        """
        Get maintenance requests that block work centers.
        Only returns requests where block_workcenter=True and stage is not done.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/maintenance_requests')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            # Check if maintenance module is installed
            maint_mod = request.env['ir.module.module'].sudo().search(
                [('name', '=', 'mrp_maintenance'), ('state', '=', 'installed')], limit=1
            )
            if not maint_mod:
                return {'success': True, 'total': 0, 'records': [], 'warning': 'mrp_maintenance module not installed'}

            cr = request.env.cr
            cr.execute("""
                SELECT mr.id, mr.name, mr.schedule_date, mr.duration,
                       mr.workcenter_id, mr.block_workcenter
                FROM maintenance_request mr
                WHERE mr.block_workcenter = TRUE
                  AND mr.workcenter_id IS NOT NULL
                  AND mr.stage_id IN (SELECT id FROM maintenance_stage WHERE done IS NOT TRUE)
                  AND mr.schedule_date IS NOT NULL
                  AND mr.company_id = %s
                ORDER BY mr.id
            """, (company_id,))

            rows = cr.dictfetchall()

            records = []
            for r in rows:
                start_date = r['schedule_date']
                duration = float(r['duration'] or 0)
                if not start_date or duration <= 0:
                    continue
                end_date = start_date + timedelta(hours=duration)

                records.append({
                    'externalId': 'maint_%d' % r['id'],
                    'name': r['name'] or 'Maintenance',
                    'workcenterExternalId': str(r['workcenter_id']),
                    'startDate': start_date.isoformat() + 'Z' if start_date else None,
                    'endDate': end_date.isoformat() + 'Z' if end_date else None,
                })

            return {
                'success': True,
                'total': len(records),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching maintenance requests')
            return {'error': 'Internal error'}

    # =========================================================================
    # CALENDARS
    # =========================================================================

    @http.route('/aps/api/v1/calendars', type='json', auth='none', methods=['POST'], csrf=False)
    def get_calendars(self, **kwargs):
        """
        Get resource calendars with working hours as weekPattern.
        Uses raw SQL for performance.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/calendars')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            cr = request.env.cr
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

            def float_to_time(hours):
                h = int(hours)
                m = int((hours - h) * 60)
                return f"{h:02d}:{m:02d}"

            # Q1: Calendars for this company (or shared)
            cr.execute("SELECT id, name, tz FROM resource_calendar WHERE company_id = %s OR company_id IS NULL ORDER BY id", (company_id,))
            cal_rows = cr.dictfetchall()

            if not cal_rows:
                return {'success': True, 'total': 0, 'records': []}

            cal_ids = [r['id'] for r in cal_rows]

            # Q2: All attendances for these calendars (batch)
            cr.execute("""
                SELECT calendar_id, dayofweek::int AS dayofweek,
                       hour_from, hour_to, day_period
                FROM resource_calendar_attendance
                WHERE calendar_id = ANY(%s)
                ORDER BY calendar_id, dayofweek, hour_from
            """, (cal_ids,))
            att_rows = cr.dictfetchall()

            # Group attendances by calendar_id
            att_by_cal = {}
            for att in att_rows:
                att_by_cal.setdefault(att['calendar_id'], []).append(att)

            records = []
            for cal in cal_rows:
                attendances = att_by_cal.get(cal['id'], [])

                # Build weekPattern
                week_pattern = {}
                day_shifts = {i: [] for i in range(7)}
                for att in attendances:
                    day_shifts[att['dayofweek']].append(att)

                for day_idx, day_name in enumerate(day_names):
                    shifts = day_shifts[day_idx]
                    week_pattern[day_name] = {
                        'shifts': [
                            {
                                'startTime': float_to_time(s['hour_from']),
                                'endTime': float_to_time(s['hour_to']),
                                'stateId': '',
                                'dayPeriod': s['day_period'],
                            }
                            for s in shifts
                        ]
                    }

                records.append({
                    'externalId': str(cal['id']),
                    'name': cal['name'],
                    'type': 'RESOURCE',
                    'timezone': cal['tz'] or 'UTC',
                    'weekPattern': week_pattern,
                })

            return {
                'success': True,
                'total': len(records),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching calendars')
            return {'error': 'Internal error'}

    # =========================================================================
    # RESOURCES (Work Centers)
    # =========================================================================

    @http.route('/aps/api/v1/resources', type='json', auth='none', methods=['POST'], csrf=False)
    def get_resources(self, **kwargs):
        """
        Get work centers as APS Resource entities.
        Uses raw SQL for performance.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/resources')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            cr = request.env.cr
            cr.execute("""
                SELECT wc.id, wc.name, wc.resource_calendar_id,
                       wc.default_capacity, wc.time_efficiency,
                       wc.costs_hour, wc.time_start, wc.time_stop
                FROM mrp_workcenter wc
                WHERE wc.active = true
                  AND wc.company_id = %s
                ORDER BY wc.id
            """, (company_id,))
            rows = cr.dictfetchall()

            wc_ids = [r['id'] for r in rows]

            # Fetch alternative workcenter relationships for this company's workcenters
            cr.execute("""
                SELECT workcenter_id, alternative_workcenter_id
                FROM mrp_workcenter_alternative_rel
                WHERE workcenter_id = ANY(%s)
            """, (wc_ids,))
            alt_rows = cr.dictfetchall()

            alt_map = {}
            for ar in alt_rows:
                wc_id = str(ar['workcenter_id'])
                alt_id = str(ar['alternative_workcenter_id'])
                alt_map.setdefault(wc_id, []).append(alt_id)

            records = []
            for r in rows:
                records.append({
                    'externalId': str(r['id']),
                    'name': r['name'],
                    'calendarExternalId': str(r['resource_calendar_id']) if r['resource_calendar_id'] else None,
                    'capacity': float(r['default_capacity'] or 1),
                    'efficiency': float(r['time_efficiency'] or 100) / 100.0,
                    'costPerHour': float(r['costs_hour'] or 0),
                    'setupTime': int(r['time_start'] or 0),
                    'cleanupTime': int(r['time_stop'] or 0),
                    'isActive': True,
                    'alternativeExternalIds': alt_map.get(str(r['id']), []),
                })

            return {
                'success': True,
                'total': len(records),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching resources')
            return {'error': 'Internal error'}

    # =========================================================================
    # PRODUCTS
    # =========================================================================

    @http.route('/aps/api/v1/products', type='json', auth='none', methods=['POST'], csrf=False)
    def get_products(self, **kwargs):
        """
        Get products in APS format.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/products')
            if not config:
                return {'error': 'Invalid or missing API key'}

            limit = min(kwargs.get('limit', 1000), 5000)
            offset = kwargs.get('offset', 0)

            env = request.env(user=SUPERUSER_ID)
            Product = env['product.product']

            # Get products that are used in manufacturing (have BOMs or are in MOs)
            domain = [('active', '=', True)]
            total = Product.search_count(domain)
            products = Product.search(domain, limit=limit, offset=offset, order='id')

            records = []
            for prod in products:
                records.append({
                    'externalId': str(prod.id),
                    'code': prod.default_code or f'PROD-{prod.id}',
                    'name': prod.name,
                    'type': map_product_type_to_aps(prod.type),
                    'unitOfMeasure': prod.uom_id.name if prod.uom_id else 'EA',
                    'isActive': prod.active,
                })

            return {
                'success': True,
                'total': total,
                'limit': limit,
                'offset': offset,
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching products')
            return {'error': 'Internal error'}

    # =========================================================================
    # BOMs (Bill of Materials) with Lines and Operations
    # =========================================================================

    @http.route('/aps/api/v1/boms', type='json', auth='none', methods=['POST'], csrf=False)
    def get_boms(self, **kwargs):
        """
        Get BOMs with embedded components (BomLines) and routing (Operations).

        Returns three arrays:
        - products: Products referenced by BOMs (parent + components)
        - bomLines: Component relationships
        - operations: Routing operations
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/boms')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            limit = min(kwargs.get('limit', 1000), 5000)
            offset = kwargs.get('offset', 0)

            env = request.env(user=SUPERUSER_ID)
            BOM = env['mrp.bom']

            domain = [
                ('active', '=', True),
                '|', ('company_id', '=', company_id), ('company_id', '=', False),
            ]
            total = BOM.search_count(domain)
            boms = BOM.search(domain, limit=limit, offset=offset, order='id')

            products = {}  # Deduplicated products
            bom_lines = []
            operations = []

            for bom in boms:
                # A BOM set on the template applies to every variant, and its
                # lines/operations can be restricted to particular attribute
                # values. Exporting the template's first variant with all lines
                # gave one variant another variant's components and left the
                # rest with no BOM at all.
                if bom.product_id:
                    variants = bom.product_id
                    suffix_ids = False
                else:
                    variants = bom.product_tmpl_id.product_variant_ids.filtered('active')
                    suffix_ids = len(variants) > 1
                    if len(variants) > MAX_BOM_VARIANTS:
                        variants = bom.product_tmpl_id.product_variant_id
                        suffix_ids = False
                        _logger.warning(
                            'BOM %s has more than %s variants, exporting only %s',
                            bom.id, MAX_BOM_VARIANTS, variants.display_name,
                        )

                for parent_product in variants:
                    products[parent_product.id] = {
                        'externalId': str(parent_product.id),
                        'code': parent_product.default_code or f'PROD-{parent_product.id}',
                        'name': parent_product.name,
                        'type': map_bom_type_to_aps(bom.type),
                        'unitOfMeasure': parent_product.uom_id.name if parent_product.uom_id else 'EA',
                        'isActive': parent_product.active,
                    }

                    # BOM Lines (components)
                    for line in bom.bom_line_ids:
                        if line._skip_bom_line(parent_product):
                            continue
                        component = line.product_id
                        products[component.id] = {
                            'externalId': str(component.id),
                            'code': component.default_code or f'PROD-{component.id}',
                            'name': component.name,
                            'type': map_product_type_to_aps(component.type),
                            'unitOfMeasure': component.uom_id.name if component.uom_id else 'EA',
                            'isActive': component.active,
                        }

                        bom_lines.append({
                            'externalId': '%s-%s' % (line.id, parent_product.id) if suffix_ids else str(line.id),
                            'parentProductExternalId': str(parent_product.id),
                            'componentProductExternalId': str(component.id),
                            'quantity': float(line.product_qty),
                            'sequence': line.sequence,
                            'unitOfMeasure': line.product_uom_id.name if line.product_uom_id else 'EA',
                        })

                    # Routing Operations
                    for op in bom.operation_ids:
                        if op._skip_operation_line(parent_product):
                            continue
                        operations.append({
                            'externalId': '%s-%s' % (op.id, parent_product.id) if suffix_ids else str(op.id),
                            'productExternalId': str(parent_product.id),
                            'resourceExternalId': str(op.workcenter_id.id),
                            'operationNumber': op.sequence,
                            'operationName': op.name,
                            'setupTime': 0,  # Odoo has setup on workcenter, not operation
                            'processTimePerUnit': float(op.time_cycle_manual),
                            'processTimePerBatch': float(op.time_mode_batch) if op.time_mode == 'auto' else 0,
                        })

            return {
                'success': True,
                'total': total,
                'products': list(products.values()),
                'bomLines': bom_lines,
                'operations': operations,
            }
        except Exception as e:
            _logger.exception('Error fetching BOMs')
            return {'error': 'Internal error'}

    # =========================================================================
    # MANUFACTURING ORDERS
    # =========================================================================

    @http.route('/aps/api/v1/orders', type='json', auth='none', methods=['POST'], csrf=False)
    def get_orders(self, **kwargs):
        """
        Get Manufacturing Orders in APS Order format.
        Uses raw SQL for performance (~3 queries instead of ~8,860 ORM round-trips).
        Parent MO resolution uses stock_move.created_production_id FK.
        SO resolution uses mrp_production.sale_id FK with procurement_group fallback.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/orders')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            limit = min(kwargs.get('limit', 1000), 5000)
            offset = kwargs.get('offset', 0)
            since = kwargs.get('since')
            statuses = kwargs.get('statuses', ['RELEASED', 'CONFIRMED', 'STARTED'])

            # Map APS statuses back to Odoo states for query
            aps_to_odoo_state = {
                'SUGGESTED': ['draft', 'cancel'],
                'RELEASED': ['confirmed'],
                'CONFIRMED': ['confirmed'],
                'STARTED': ['progress', 'to_close'],
                'COMPLETE': ['done'],
            }
            odoo_states = []
            for status in statuses:
                odoo_states.extend(aps_to_odoo_state.get(status, []))
            odoo_states = list(set(odoo_states))

            cr = request.env.cr

            # Build dynamic WHERE
            where_clauses = ["mp.state = ANY(%s)", "mp.company_id = %s"]
            params = [odoo_states, company_id]
            if since:
                where_clauses.append("mp.write_date >= %s")
                params.append(since)
            where_sql = " AND ".join(where_clauses)

            # Count total
            cr.execute(f"SELECT count(*) FROM mrp_production mp WHERE {where_sql}", params)
            total = cr.fetchone()[0]

            # Q1: MOs with product, parent MO (via stock_move FK)
            cr.execute(f"""
                SELECT mp.id, mp.name, mp.product_id, mp.product_qty, mp.qty_producing,
                       mp.date_deadline, mp.date_start, mp.priority, mp.state, mp.origin,
                       pp.id AS prod_pp_id,
                       COALESCE(pt.name->>'en_US', (SELECT value FROM jsonb_each_text(pt.name) LIMIT 1), '') AS product_name,
                       pp.default_code AS product_code,
                       pt.type AS product_type, pp.active AS product_active,
                       COALESCE(uom.name->>'en_US', (SELECT value FROM jsonb_each_text(uom.name) LIMIT 1), 'EA') AS product_uom,
                       parent_mo.id AS parent_mo_id
                FROM mrp_production mp
                JOIN product_product pp ON pp.id = mp.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                LEFT JOIN uom_uom uom ON uom.id = pt.uom_id
                LEFT JOIN LATERAL (
                    SELECT DISTINCT sm.raw_material_production_id AS id
                    FROM stock_move sm
                    WHERE sm.created_production_id = mp.id
                      AND sm.raw_material_production_id IS NOT NULL
                      AND sm.state != 'cancel'
                    LIMIT 1
                ) parent_mo ON true
                WHERE {where_sql}
                ORDER BY mp.id
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            mo_rows = cr.dictfetchall()

            if not mo_rows:
                return {
                    'success': True, 'total': total, 'limit': limit,
                    'offset': offset, 'products': [], 'records': [],
                }

            mo_ids = [r['id'] for r in mo_rows]

            # Q2: Components (stock moves) for this page of MOs.
            # stock_move.quantity only exists from Odoo 17 onwards under that
            # name, so check before selecting it — this module ships for 17/18/19.
            cr.execute("""SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'stock_move' AND column_name = 'quantity'""")
            reserved_col = 'sm.quantity' if cr.fetchone() else '0.0'
            cr.execute(f"""
                SELECT sm.id, sm.raw_material_production_id AS mo_id,
                       sm.product_id, sm.product_uom_qty, sm.state,
                       sm.operation_id, sm.workorder_id,
                       {reserved_col} AS reserved_qty,
                       pp.default_code AS product_code,
                       COALESCE(pt.name->>'en_US', (SELECT value FROM jsonb_each_text(pt.name) LIMIT 1), '') AS product_name,
                       pt.type AS product_type, pp.active AS product_active,
                       COALESCE(uom.name->>'en_US', (SELECT value FROM jsonb_each_text(uom.name) LIMIT 1), 'EA') AS product_uom
                FROM stock_move sm
                JOIN product_product pp ON pp.id = sm.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                LEFT JOIN uom_uom uom ON uom.id = sm.product_uom
                WHERE sm.raw_material_production_id = ANY(%s)
                  AND sm.state != 'cancel'
                ORDER BY sm.raw_material_production_id, sm.id
            """, (mo_ids,))
            comp_rows = cr.dictfetchall()

            # Group components by MO id
            comps_by_mo = {}
            for c in comp_rows:
                comps_by_mo.setdefault(c['mo_id'], []).append(c)

            # Q3: SO lookup — three standard Odoo paths
            so_by_mo = {}  # mo_id -> (so_name, customer_name)
            cr.execute("""
                WITH RECURSIVE
                -- Path A: created_production_id → PG → sale_id (base)
                -- Path B: move_dest_ids → PG → sale_id (Odoo MO smart button)
                -- Then recurse down to children via stock_move chain
                mo_so AS (
                    -- A: stock_move.created_production_id → move's PG → sale_id
                    SELECT mp.id, so.name AS sale_order_name, rp.name AS customer_name
                    FROM mrp_production mp
                    JOIN stock_move sm ON sm.created_production_id = mp.id
                    JOIN procurement_group pg ON pg.id = sm.group_id
                        AND pg.sale_id IS NOT NULL
                    JOIN sale_order so ON so.id = pg.sale_id
                    LEFT JOIN res_partner rp ON rp.id = so.partner_id
                    WHERE mp.id = ANY(%(ids)s)

                    UNION

                    -- B: MO → siblings in PG → finished move → move_dest → PG → sale_id
                    SELECT mp.id, so.name, rp.name
                    FROM mrp_production mp
                    JOIN mrp_production sibling
                        ON sibling.procurement_group_id = mp.procurement_group_id
                    JOIN stock_move sm ON sm.production_id = sibling.id
                    JOIN stock_move_move_rel rel ON rel.move_orig_id = sm.id
                    JOIN stock_move dest ON dest.id = rel.move_dest_id
                    JOIN procurement_group pg ON pg.id = dest.group_id
                        AND pg.sale_id IS NOT NULL
                    JOIN sale_order so ON so.id = pg.sale_id
                    LEFT JOIN res_partner rp ON rp.id = so.partner_id
                    WHERE mp.id = ANY(%(ids)s)

                    UNION ALL

                    -- Recurse: children via stock_move chain
                    SELECT child.id, parent_so.sale_order_name, parent_so.customer_name
                    FROM mo_so parent_so
                    JOIN stock_move sm ON sm.raw_material_production_id = parent_so.id
                        AND sm.created_production_id IS NOT NULL
                        AND sm.state != 'cancel'
                    JOIN mrp_production child ON child.id = sm.created_production_id
                    WHERE child.id = ANY(%(ids)s)
                ),
                -- Path C: origin chain fallback for MOs not found above
                origin_chain AS (
                    SELECT mp.id AS original_id, mp.id AS current_id, mp.origin
                    FROM mrp_production mp
                    WHERE mp.id = ANY(%(ids)s)
                      AND mp.id NOT IN (SELECT id FROM mo_so)
                      AND mp.origin IS NOT NULL AND mp.origin != ''

                    UNION ALL

                    SELECT oc.original_id, parent.id, parent.origin
                    FROM origin_chain oc
                    JOIN mrp_production parent ON parent.name = oc.origin
                    WHERE oc.origin LIKE 'WH/MO/%%'
                ),
                origin_so AS (
                    SELECT DISTINCT ON (oc.original_id)
                        oc.original_id AS id,
                        COALESCE(so_pg.name, so_origin.name) AS sale_order_name,
                        COALESCE(rp_pg.name, rp_origin.name) AS customer_name
                    FROM origin_chain oc
                    JOIN mrp_production ancestor ON ancestor.id = oc.current_id
                    -- Try move_dest_ids path on ancestor
                    LEFT JOIN mrp_production anc_sibling
                        ON anc_sibling.procurement_group_id = ancestor.procurement_group_id
                    LEFT JOIN stock_move anc_sm ON anc_sm.production_id = anc_sibling.id
                    LEFT JOIN stock_move_move_rel anc_rel ON anc_rel.move_orig_id = anc_sm.id
                    LEFT JOIN stock_move anc_dest ON anc_dest.id = anc_rel.move_dest_id
                    LEFT JOIN procurement_group pg ON pg.id = anc_dest.group_id
                        AND pg.sale_id IS NOT NULL
                    LEFT JOIN sale_order so_pg ON so_pg.id = pg.sale_id
                    LEFT JOIN res_partner rp_pg ON rp_pg.id = so_pg.partner_id
                    -- Try origin = SO name (exact or prefix before '/')
                    LEFT JOIN sale_order so_origin
                        ON so_origin.name = ancestor.origin
                        OR so_origin.name = split_part(ancestor.origin, '/', 1)
                    LEFT JOIN res_partner rp_origin ON rp_origin.id = so_origin.partner_id
                    WHERE so_pg.id IS NOT NULL OR so_origin.id IS NOT NULL
                    ORDER BY oc.original_id
                )
                SELECT id, sale_order_name, customer_name FROM mo_so
                UNION
                SELECT id, sale_order_name, customer_name FROM origin_so
            """, {'ids': mo_ids})
            for row in cr.dictfetchall():
                so_by_mo[row['id']] = (row['sale_order_name'], row['customer_name'])

            # Build response
            products = {}
            records = []

            for mo in mo_rows:
                # Add MO product
                prod_id = mo['prod_pp_id']
                if prod_id not in products:
                    products[prod_id] = {
                        'externalId': str(prod_id),
                        'code': mo['product_code'] or f'PROD-{prod_id}',
                        'name': mo['product_name'],
                        'type': 'MANUFACTURED',
                        'unitOfMeasure': mo['product_uom'] or 'EA',
                        'isActive': mo['product_active'],
                    }

                # SO info via procurement_group
                sale_order_name = None
                customer_name = None
                if mo['id'] in so_by_mo:
                    sale_order_name, customer_name = so_by_mo[mo['id']]

                # Parent MO from stock_move FK
                parent_mo_external_id = str(mo['parent_mo_id']) if mo['parent_mo_id'] else None

                # Components
                components = []
                for c in comps_by_mo.get(mo['id'], []):
                    c_prod_id = c['product_id']
                    if c_prod_id not in products:
                        products[c_prod_id] = {
                            'externalId': str(c_prod_id),
                            'code': c['product_code'] or f'PROD-{c_prod_id}',
                            'name': c['product_name'],
                            'type': map_product_type_to_aps(c['product_type']),
                            'unitOfMeasure': c['product_uom'] or 'EA',
                            'isActive': c['product_active'],
                        }
                    components.append({
                        'externalId': str(c['id']),
                        'productExternalId': str(c_prod_id),
                        'quantity': float(c['product_uom_qty']),
                        'unitOfMeasure': c['product_uom'] or 'EA',
                        # Which routing step actually consumes this — without it
                        # every requirement lands on the MO's first operation
                        'workOrderExternalId': str(c['workorder_id']) if c['workorder_id'] else None,
                        'operationExternalId': str(c['operation_id']) if c['operation_id'] else None,
                        'quantityReserved': float(c['reserved_qty'] or 0),
                        'isReserved': c['state'] == 'assigned',
                    })

                records.append({
                    'externalId': str(mo['id']),
                    'orderNumber': mo['name'],
                    'productExternalId': str(prod_id),
                    'quantity': float(mo['product_qty']),
                    'quantityCompleted': float(mo['qty_producing'] or 0),
                    'dueDate': mo['date_deadline'].isoformat() + 'Z' if mo['date_deadline'] else None,
                    'earliestStartDate': mo['date_start'].isoformat() + 'Z' if mo['date_start'] else None,
                    'priority': map_priority_to_aps(mo['priority']),
                    'status': map_mo_state_to_aps(mo['state']),
                    'locked': False,
                    'origin': mo['origin'],
                    'saleOrderName': sale_order_name,
                    'customerName': customer_name,
                    'parentMoExternalId': parent_mo_external_id,
                    'components': components,
                })

            return {
                'success': True,
                'total': total,
                'limit': limit,
                'offset': offset,
                'products': list(products.values()),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching orders')
            return {'error': 'Internal error'}

    # =========================================================================
    # WORK ORDERS (Order Operations)
    # =========================================================================

    @http.route('/aps/api/v1/operations', type='json', auth='none', methods=['POST'], csrf=False)
    def get_operations(self, **kwargs):
        """
        Get Work Orders as APS OrderOperation entities.
        Uses raw SQL for performance.
        Dependencies: reads mrp_workorder_dependencies_rel, then falls back to
        sequence-based chain for MOs with zero explicit deps.
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/operations')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            limit = min(kwargs.get('limit', 1000), 5000)
            offset = kwargs.get('offset', 0)
            since = kwargs.get('since')
            order_external_ids = kwargs.get('orderExternalIds')
            statuses = kwargs.get('statuses', ['PENDING', 'SCHEDULED', 'IN_PROGRESS'])

            # Map APS statuses to Odoo states
            aps_to_odoo_state = {
                'PENDING': ['pending', 'waiting'],
                'SCHEDULED': ['ready'],
                'IN_PROGRESS': ['progress'],
                'COMPLETE': ['done'],
                'CANCELLED': ['cancel'],
            }
            odoo_states = []
            for status in statuses:
                odoo_states.extend(aps_to_odoo_state.get(status, []))
            odoo_states = list(set(odoo_states))

            cr = request.env.cr

            # Build dynamic WHERE (exclude WOs whose parent MO is draft/cancelled)
            where_clauses = ["wo.state = ANY(%s)", "mp.state NOT IN ('draft', 'cancel')", "mp.company_id = %s"]
            params = [odoo_states, company_id]
            if since:
                where_clauses.append("wo.write_date >= %s")
                params.append(since)
            if order_external_ids:
                where_clauses.append("wo.production_id = ANY(%s)")
                params.append([int(oid) for oid in order_external_ids])
            where_sql = " AND ".join(where_clauses)

            # Count total
            cr.execute(f"SELECT count(*) FROM mrp_workorder wo JOIN mrp_production mp ON mp.id = wo.production_id WHERE {where_sql}", params)
            total = cr.fetchone()[0]

            # Q1: WOs with sequence from routing operation
            cr.execute(f"""
                SELECT wo.id, wo.production_id, wo.workcenter_id, wo.operation_id,
                       wo.name, wo.duration_expected, wo.duration, wo.qty_produced,
                       wo.state, wo.write_date, wo.date_start, wo.date_finished,
                       COALESCE(mro.sequence, 10) AS op_sequence,
                       mp.qty_producing AS mo_qty_producing,
                       mp.product_qty AS mo_product_qty
                FROM mrp_workorder wo
                JOIN mrp_production mp ON mp.id = wo.production_id
                LEFT JOIN mrp_routing_workcenter mro ON mro.id = wo.operation_id
                WHERE {where_sql}
                ORDER BY wo.id
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            wo_rows = cr.dictfetchall()

            if not wo_rows:
                return {
                    'success': True, 'total': total, 'limit': limit,
                    'offset': offset, 'records': [],
                }

            wo_ids = [r['id'] for r in wo_rows]

            # Q2: Explicit dependencies (batch)
            cr.execute("""
                SELECT workorder_id, blocked_by_id
                FROM mrp_workorder_dependencies_rel
                WHERE workorder_id = ANY(%s)
            """, (wo_ids,))
            dep_rows = cr.fetchall()

            # Group explicit deps by workorder_id
            explicit_deps = {}  # wo_id -> set of blocked_by_ids
            for wo_id, blocked_by_id in dep_rows:
                explicit_deps.setdefault(wo_id, set()).add(blocked_by_id)

            # Identify which MOs in this batch have ANY explicit deps
            # (check per-MO, not per-WO: if even one WO in the MO has deps, use explicit)
            mo_ids_with_explicit_deps = set()
            for wo_id, dep_set in explicit_deps.items():
                if dep_set:
                    # Find which MO this WO belongs to
                    for r in wo_rows:
                        if r['id'] == wo_id:
                            mo_ids_with_explicit_deps.add(r['production_id'])
                            break

            # Build sequence-based fallback for MOs with ZERO explicit deps
            # Group WOs by production_id
            wos_by_mo = {}
            for r in wo_rows:
                wos_by_mo.setdefault(r['production_id'], []).append(r)

            seq_deps = {}  # wo_id -> list of blocked_by_ids (from sequence logic)
            for mo_id, mo_wos in wos_by_mo.items():
                if mo_id in mo_ids_with_explicit_deps:
                    continue  # This MO has explicit deps, skip
                if len(mo_wos) <= 1:
                    continue  # Single WO, no deps needed

                # Sort by sequence
                mo_wos_sorted = sorted(mo_wos, key=lambda w: w['op_sequence'])

                # Group by sequence value (same sequence = parallel ops)
                seq_groups = []
                current_seq = None
                current_group = []
                for w in mo_wos_sorted:
                    if w['op_sequence'] != current_seq:
                        if current_group:
                            seq_groups.append(current_group)
                        current_group = [w]
                        current_seq = w['op_sequence']
                    else:
                        current_group.append(w)
                if current_group:
                    seq_groups.append(current_group)

                # Each sequence group depends on all WOs in the previous group
                for i in range(1, len(seq_groups)):
                    prev_ids = [w['id'] for w in seq_groups[i - 1]]
                    for w in seq_groups[i]:
                        seq_deps[w['id']] = prev_ids

            # Build records
            # Set of all WO IDs in this batch (for filtering deps to only include WOs in batch)
            wo_id_set = set(wo_ids)

            records = []
            for wo in wo_rows:
                wo_id = wo['id']
                mo_id = wo['production_id']

                # Determine blocked_by list
                if mo_id in mo_ids_with_explicit_deps:
                    # Use explicit deps (filter to only WOs in this batch)
                    blocked_by = [str(bid) for bid in explicit_deps.get(wo_id, set())
                                  if bid in wo_id_set]
                else:
                    # Use sequence-based deps
                    blocked_by = [str(bid) for bid in seq_deps.get(wo_id, [])]

                expected = float(wo['duration_expected'] or 0)
                real = float(wo['duration'] or 0)
                # Two independent signals of how much is left: time already
                # logged, and quantity already produced. Trust whichever says
                # there is less work to do — over-planning an operation that is
                # nearly finished is what pushes the rest of the order out.
                # The MO's ordered quantity, not what is being produced in the
                # current run — qty_producing is a slice, not the total
                total_qty = float(wo['mo_product_qty'] or 0)
                done_qty = float(wo['qty_produced'] or 0)
                remaining = expected - real
                if total_qty > 0 and done_qty > 0:
                    remaining = min(remaining, expected * max(total_qty - done_qty, 0) / total_qty)
                records.append({
                    'externalId': str(wo_id),
                    'orderExternalId': str(mo_id),
                    'resourceExternalId': str(wo['workcenter_id']),
                    'operationExternalId': str(wo['operation_id']) if wo['operation_id'] else None,
                    'operationNumber': wo['op_sequence'],
                    'operationName': wo['name'],
                    'operationStart': wo['date_start'].isoformat() + 'Z' if wo['date_start'] else None,
                    'operationEnd': wo['date_finished'].isoformat() + 'Z' if wo['date_finished'] else None,
                    'setupTime': 0,
                    # Work already logged is done — planning the full expected
                    # duration again pushes the finish out for work that is
                    # nearly complete. Never zero, or the WO drops off the plan.
                    'processTime': round(max(remaining, 1.0) if wo['state'] == 'progress' else expected, 2),
                    'expectedDuration': round(expected, 2),
                    'actualDuration': round(real, 2),
                    'quantity': float(wo['mo_qty_producing'] or 0),
                    'quantityCompleted': float(wo['qty_produced'] or 0),
                    'status': map_wo_state_to_aps(wo['state']),
                    'locked': wo['state'] == 'done',
                    'writeDate': wo['write_date'].isoformat() + 'Z' if wo['write_date'] else None,
                    'blockedByExternalIds': blocked_by,
                })

            return {
                'success': True,
                'total': total,
                'limit': limit,
                'offset': offset,
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching operations')
            return {'error': 'Internal error'}

    # =========================================================================
    # INVENTORY (Material Supply - Stock)
    # =========================================================================

    @http.route('/aps/api/v1/inventory', type='json', auth='none', methods=['POST'], csrf=False)
    def get_inventory(self, **kwargs):
        """
        Get inventory as APS MaterialSupply entities (type=INVENTORY).
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/inventory')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            product_external_ids = kwargs.get('productExternalIds')
            location_external_ids = kwargs.get('locationExternalIds')

            env = request.env(user=SUPERUSER_ID)
            Quant = env['stock.quant']

            # Only real stock counts as supply — scrap, production and customer
            # locations were being reported as available material
            domain = [
                ('quantity', '>', 0),
                ('company_id', '=', company_id),
                ('location_id.usage', 'in', ('internal', 'transit')),
            ]
            if product_external_ids:
                domain.append(('product_id', 'in', [int(pid) for pid in product_external_ids]))
            if location_external_ids:
                domain.append(('location_id', 'in', [int(lid) for lid in location_external_ids]))

            quants = Quant.search(domain)

            # Collect products
            products = {}
            records = []

            for quant in quants:
                product = quant.product_id
                products[product.id] = {
                    'externalId': str(product.id),
                    'code': product.default_code or f'PROD-{product.id}',
                    'name': product.name,
                    'type': map_product_type_to_aps(product.type),
                    'unitOfMeasure': product.uom_id.name if product.uom_id else 'EA',
                    'isActive': product.active,
                }

                records.append({
                    'externalId': str(quant.id),
                    'productExternalId': str(product.id),
                    'supplyType': 'INVENTORY',
                    'quantity': float(quant.quantity),
                    'quantityReserved': float(quant.reserved_quantity),
                    # Reservations can exceed what is physically there after an
                    # adjustment; a negative supply row is not a supply
                    'quantityAvailable': max(0.0, float(quant.quantity - quant.reserved_quantity)),
                    # On hand means on hand. Sending "now" moved existing stock's
                    # availability forward on every sync.
                    'availableDate': '1970-01-01T00:00:00Z',
                    'locationExternalId': str(quant.location_id.id),
                    'locationName': quant.location_id.complete_name,
                })

            return {
                'success': True,
                'total': len(records),
                'products': list(products.values()),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching inventory')
            return {'error': 'Internal error'}

    # =========================================================================
    # PURCHASE ORDERS (Material Supply - PO)
    # =========================================================================

    @http.route('/aps/api/v1/purchase_orders', type='json', auth='none', methods=['POST'], csrf=False)
    def get_purchase_orders(self, **kwargs):
        """
        Get open purchase order lines as APS MaterialSupply entities (type=PURCHASE_ORDER).
        """
        try:
            api_key = kwargs.get('api_key')
            config = validate_api_key(api_key, '/purchase_orders')
            if not config:
                return {'error': 'Invalid or missing API key'}
            company_id = config.company_id.id

            product_external_ids = kwargs.get('productExternalIds')

            env = request.env(user=SUPERUSER_ID)
            POLine = env['purchase.order.line']

            # Note: Cannot compare qty_received < product_qty in domain (Odoo limitation)
            # So we filter by state only and then filter by quantity in Python
            domain = [
                ('order_id.state', 'in', ['purchase', 'done']),
                ('company_id', '=', company_id),
            ]
            if product_external_ids:
                domain.append(('product_id', 'in', [int(pid) for pid in product_external_ids]))

            all_lines = POLine.search(domain)
            # Filter to only include lines with pending quantity
            lines = all_lines.filtered(lambda l: l.qty_received < l.product_qty)

            # Collect products
            products = {}
            records = []

            for line in lines:
                product = line.product_id
                products[product.id] = {
                    'externalId': str(product.id),
                    'code': product.default_code or f'PROD-{product.id}',
                    'name': product.name,
                    'type': map_product_type_to_aps(product.type),
                    'unitOfMeasure': product.uom_id.name if product.uom_id else 'EA',
                    'isActive': product.active,
                }

                # A line without a planned date used to be sent as null, which
                # APS read as 1970 — i.e. "already here"
                planned = (line.date_planned or line.order_id.date_planned
                           or line.order_id.date_approve or line.order_id.date_order)

                qty_pending = float(line.product_qty) - float(line.qty_received)
                records.append({
                    'externalId': str(line.id),
                    'productExternalId': str(product.id),
                    'supplyType': 'PURCHASE_ORDER',
                    'quantity': float(line.product_qty),
                    'quantityReserved': float(line.qty_received),  # Already received
                    'quantityAvailable': qty_pending,
                    'availableDate': planned.isoformat() + 'Z' if planned else None,
                    'availableDateKnown': bool(planned),
                    'referenceNumber': line.order_id.name,
                    'supplierExternalId': str(line.order_id.partner_id.id),
                    'supplierName': line.order_id.partner_id.name,
                })

            return {
                'success': True,
                'total': len(records),
                'products': list(products.values()),
                'records': records,
            }
        except Exception as e:
            _logger.exception('Error fetching purchase orders')
            return {'error': 'Internal error'}

    # =========================================================================
    # SCHEDULE WRITE-BACK (APS → Odoo)
    # =========================================================================

    @http.route('/aps/api/v1/schedule/write_back', type='json', auth='none', methods=['POST'], csrf=False)
    def write_back_schedule(self, **kwargs):
        """
        Write scheduled dates back to work orders.

        Expects APS format:
        {
            "apiKey": "xxx",
            "operations": [
                {
                    "externalId": "1",
                    "operationStart": "2024-01-15T08:00:00Z",
                    "operationEnd": "2024-01-15T12:00:00Z",
                    "resourceExternalId": "5",  // optional - for resource reassignment
                    "syncedWriteDate": "2024-01-14T10:00:00Z"  // optional - for conflict detection
                }
            ],
            "dryRun": false,
            "conflictStrategy": "SKIP_CONFLICTS"  // ABORT, SKIP_CONFLICTS, FORCE_OVERWRITE
        }
        """
        try:
            api_key = kwargs.get('apiKey') or kwargs.get('api_key')
            config = validate_api_key(api_key, '/schedule/write_back')
            if not config:
                return {'error': 'Invalid or missing API key'}

            operations_data = kwargs.get('operations', [])
            dry_run = kwargs.get('dryRun', False)
            conflict_strategy = kwargs.get('conflictStrategy', 'SKIP_CONFLICTS')

            if not operations_data:
                return {'error': 'No operations provided'}

            company_id = config.company_id.id
            env = request.env(user=SUPERUSER_ID)
            Workorder = env['mrp.workorder']
            Production = env['mrp.production']

            results = {
                'success': True,
                'dryRun': dry_run,
                'conflictStrategy': conflict_strategy,
                'updated': 0,
                'failed': 0,
                'conflicts': [],
                'errors': [],
                'mosUpdated': 0,
            }

            # Track affected MOs to update their dates after all WOs are processed
            affected_mo_ids = set()

            # First pass: detect conflicts if syncedWriteDate is provided
            operations_to_update = []
            for op_data in operations_data:
                external_id = op_data.get('externalId')
                synced_write_date = op_data.get('syncedWriteDate')

                if not external_id:
                    results['errors'].append({'error': 'Missing externalId'})
                    results['failed'] += 1
                    continue

                workorder = Workorder.browse(int(external_id))
                if not workorder.exists() or workorder.company_id.id != company_id:
                    results['errors'].append({
                        'externalId': external_id,
                        'error': 'Work order not found',
                    })
                    results['failed'] += 1
                    continue

                # Check for conflict if syncedWriteDate is provided
                has_conflict = False
                if synced_write_date:
                    # Parse synced write date
                    synced_str = synced_write_date.replace('Z', '').replace('T', ' ')
                    if '.' in synced_str:
                        synced_str = synced_str.split('.')[0]
                    synced_dt = fields.Datetime.from_string(synced_str)

                    # Compare with current write_date
                    if workorder.write_date and workorder.write_date > synced_dt:
                        has_conflict = True
                        conflict_info = {
                            'externalId': external_id,
                            'displayName': f'{workorder.production_id.name}: {workorder.name}',
                            'syncedWriteDate': synced_write_date,
                            'currentWriteDate': workorder.write_date.isoformat() + 'Z',
                            'apsValues': {
                                'operationStart': op_data.get('operationStart'),
                                'operationEnd': op_data.get('operationEnd'),
                                'resourceExternalId': op_data.get('resourceExternalId'),
                            },
                        }
                        results['conflicts'].append(conflict_info)

                        if conflict_strategy == 'ABORT':
                            results['success'] = False
                            results['errors'].append({
                                'externalId': external_id,
                                'error': 'Conflict detected - record modified since sync',
                            })
                            # Don't process any more operations
                            return results
                        elif conflict_strategy == 'SKIP_CONFLICTS':
                            results['failed'] += 1
                            continue  # Skip this operation
                        # FORCE_OVERWRITE: continue processing

                operations_to_update.append((op_data, workorder))

            # Second pass: update non-conflicting operations
            for op_data, workorder in operations_to_update:
                try:
                    operation_start = op_data.get('operationStart')
                    operation_end = op_data.get('operationEnd')
                    resource_external_id = op_data.get('resourceExternalId')

                    # Parse dates (ISO format: 2026-01-28T08:00:00.000Z)
                    start_dt = None
                    end_dt = None
                    if operation_start:
                        # Remove Z and replace T with space for Odoo
                        start_str = operation_start.replace('Z', '').replace('T', ' ')
                        # Remove milliseconds if present
                        if '.' in start_str:
                            start_str = start_str.split('.')[0]
                        start_dt = fields.Datetime.from_string(start_str)
                    if operation_end:
                        end_str = operation_end.replace('Z', '').replace('T', ' ')
                        if '.' in end_str:
                            end_str = end_str.split('.')[0]
                        end_dt = fields.Datetime.from_string(end_str)
                    workcenter_id = int(resource_external_id) if resource_external_id else None

                    if not dry_run:
                        workorder.write_aps_schedule(start_dt, end_dt, workcenter_id)
                        # Track affected MO for date update
                        if workorder.production_id:
                            affected_mo_ids.add(workorder.production_id.id)

                    results['updated'] += 1

                except Exception as e:
                    _logger.warning('Write-back error for %s: %s', op_data.get('externalId'), str(e))
                    results['errors'].append({
                        'externalId': op_data.get('externalId'),
                        'error': 'Failed to update work order',  # Don't expose internal details
                    })
                    results['failed'] += 1

            if results['failed'] > 0 and results['updated'] == 0:
                results['success'] = False

            # Update MO dates based on their work orders (mimics Plan button behavior)
            if not dry_run and affected_mo_ids:
                for mo in Production.browse(list(affected_mo_ids)):
                    try:
                        if mo.update_dates_from_workorders():
                            results['mosUpdated'] += 1
                    except Exception as e:
                        _logger.warning('Failed to update MO %s dates: %s', mo.name, str(e))

            return results

        except Exception as e:
            _logger.exception('Error writing back schedule')
            return {'error': 'Internal error'}

    # =========================================================================
    # SCHEDULE VALIDATION
    # =========================================================================

    @http.route('/aps/api/v1/schedule/validate', type='json', auth='none', methods=['POST'], csrf=False)
    def validate_schedule(self, **kwargs):
        """
        Validate schedule before write-back.
        """
        try:
            api_key = kwargs.get('apiKey') or kwargs.get('api_key')
            config = validate_api_key(api_key, '/schedule/validate')
            if not config:
                return {'error': 'Invalid or missing API key'}

            company_id = config.company_id.id
            operations_data = kwargs.get('operations', [])

            env = request.env(user=SUPERUSER_ID)
            Workorder = env['mrp.workorder']
            Workcenter = env['mrp.workcenter']

            issues = []

            # Collect all IDs for batch validation
            wo_ids = [int(op.get('externalId')) for op in operations_data if op.get('externalId')]
            wc_ids = [int(op.get('resourceExternalId')) for op in operations_data if op.get('resourceExternalId')]

            # Validate work orders exist and belong to this company
            existing_wos = Workorder.browse(wo_ids).exists().filtered(
                lambda w: w.company_id.id == company_id
            )
            existing_wo_ids = set(existing_wos.ids)

            for op_data in operations_data:
                external_id = op_data.get('externalId')
                if external_id and int(external_id) not in existing_wo_ids:
                    issues.append({
                        'type': 'OPERATION_NOT_FOUND',
                        'externalId': external_id,
                        'message': f'Work order {external_id} does not exist',
                    })

            # Validate work centers exist and belong to this company
            if wc_ids:
                existing_wcs = Workcenter.browse(list(set(wc_ids))).exists().filtered(
                    lambda w: w.company_id.id == company_id or not w.company_id
                )
                existing_wc_ids = set(existing_wcs.ids)

                for op_data in operations_data:
                    resource_id = op_data.get('resourceExternalId')
                    if resource_id and int(resource_id) not in existing_wc_ids:
                        issues.append({
                            'type': 'RESOURCE_NOT_FOUND',
                            'resourceExternalId': resource_id,
                            'operationExternalId': op_data.get('externalId'),
                            'message': f'Work center {resource_id} does not exist',
                        })

            return {
                'valid': len(issues) == 0,
                'issueCount': len(issues),
                'issues': issues,
            }

        except Exception as e:
            _logger.exception('Error validating schedule')
            return {'error': 'Internal error'}

    # =========================================================================
    # MATERIAL RE-RESERVATION - Unreserve and re-reserve for new schedule
    # =========================================================================

    @http.route('/aps/api/v1/materials/re_reserve', type='json', auth='none', methods=['POST'], csrf=False)
    def re_reserve_materials(self, **kwargs):
        """
        Global two-phase material re-reservation after schedule publish.

        Phase 1: Bulk unreserve all eligible (non-frozen) MOs to release quants
        Phase 2: Re-reserve by priority groups so high-priority MOs claim materials first

        Freeze window: MOs whose first non-done WO starts within freezeHorizonDays
        keep their existing reservations untouched.

        Accepts two formats:
        - priorityGroups: [{priority: 1, orderExternalIds: ["101", "102"]}, ...]  (batched)
        - orderExternalIds: ["101", "102", ...]  (flat, legacy)
        """
        try:
            api_key = kwargs.get('apiKey') or kwargs.get('api_key')
            config = validate_api_key(api_key, '/materials/re_reserve')
            if not config:
                return {'error': 'Invalid or missing API key'}

            priority_groups = kwargs.get('priorityGroups', [])
            order_external_ids = kwargs.get('orderExternalIds', [])
            freeze_horizon_days = kwargs.get('freezeHorizonDays', 0)
            dry_run = kwargs.get('dryRun', False)

            # Build flat list of all external IDs from either format
            if priority_groups:
                all_ext_ids = []
                for group in priority_groups:
                    all_ext_ids.extend(group.get('orderExternalIds', []))
            elif order_external_ids:
                all_ext_ids = list(order_external_ids)
            else:
                return {'error': 'priorityGroups or orderExternalIds is required'}

            company_id = config.company_id.id
            env = request.env(user=SUPERUSER_ID)
            Production = env['mrp.production']

            now = datetime.utcnow()
            freeze_cutoff = now + timedelta(days=freeze_horizon_days)

            summary = {
                'totalMOs': len(all_ext_ids),
                'frozenMOs': 0,
                'eligibleMOs': 0,
                'unreserved': 0,
                'reReserved': 0,
                'skippedDoneCancel': 0,
                'notFound': 0,
                'errors': 0,
            }
            frozen_orders = []
            reservation_status = []

            # Batch browse all MOs at once
            all_mo_int_ids = []
            ext_id_set = set()
            for ext_id in all_ext_ids:
                try:
                    all_mo_int_ids.append(int(ext_id))
                    ext_id_set.add(str(ext_id))
                except (ValueError, TypeError):
                    summary['errors'] += 1
                    reservation_status.append({
                        'orderExternalId': str(ext_id),
                        'action': 'ERROR',
                        'error': 'Invalid external ID',
                    })

            all_mos = Production.browse(all_mo_int_ids).exists().filtered(
                lambda mo: mo.company_id.id == company_id
            )
            mo_by_id = {str(mo.id): mo for mo in all_mos}

            # Track which ext_ids were found
            found_ids = set(mo_by_id.keys())
            eligible_ext_ids = set()  # ext_ids eligible for re-reservation

            # Classify all MOs
            for ext_id in all_ext_ids:
                ext_id_str = str(ext_id)
                if ext_id_str not in found_ids:
                    if ext_id_str in ext_id_set:  # Valid int but not found
                        summary['notFound'] += 1
                        reservation_status.append({
                            'orderExternalId': ext_id_str,
                            'action': 'NOT_FOUND',
                            'reason': 'MO not found in Odoo',
                        })
                    continue

                mo = mo_by_id[ext_id_str]

                # Skip done/cancel MOs
                if mo.state in ('done', 'cancel'):
                    summary['skippedDoneCancel'] += 1
                    reservation_status.append({
                        'orderExternalId': ext_id_str,
                        'orderNumber': mo.name,
                        'action': 'SKIPPED',
                        'reason': 'MO state is %s' % mo.state,
                    })
                    continue

                # Check freeze window: earliest non-done WO date_start
                earliest_wo_start = None
                for wo in mo.workorder_ids:
                    if wo.state not in ('done', 'cancel') and wo.date_start:
                        if earliest_wo_start is None or wo.date_start < earliest_wo_start:
                            earliest_wo_start = wo.date_start

                if earliest_wo_start and earliest_wo_start <= freeze_cutoff:
                    summary['frozenMOs'] += 1
                    res_state = mo.reservation_state if hasattr(mo, 'reservation_state') else 'unknown'
                    frozen_orders.append({
                        'orderExternalId': ext_id_str,
                        'orderNumber': mo.name,
                        'earliestWoStart': earliest_wo_start.isoformat() + 'Z',
                        'reservationState': res_state,
                    })
                    continue

                # Eligible for re-reservation
                summary['eligibleMOs'] += 1
                eligible_ext_ids.add(ext_id_str)

            if not dry_run:
                # Phase 1: Bulk unreserve all eligible MOs in one batch
                eligible_mo_int_ids = [int(eid) for eid in eligible_ext_ids]
                if eligible_mo_int_ids:
                    try:
                        all_moves = env['stock.move'].search([
                            ('raw_material_production_id', 'in', eligible_mo_int_ids),
                            ('state', 'not in', ('done', 'cancel'))
                        ])
                        all_moves._do_unreserve()
                        summary['unreserved'] = len(eligible_ext_ids)
                    except Exception as e:
                        summary['errors'] += len(eligible_ext_ids)
                        _logger.warning('Bulk unreserve error: %s', str(e))
                        reservation_status.append({
                            'orderExternalId': 'BATCH',
                            'action': 'ERROR',
                            'error': 'Bulk unreserve failed: %s' % str(e),
                        })
                        # Cannot proceed to re-reserve if unreserve failed
                        eligible_ext_ids = set()

                # Phase 2: Re-reserve by priority groups
                if priority_groups and eligible_ext_ids:
                    for group in priority_groups:
                        group_ext_ids = [str(eid) for eid in group.get('orderExternalIds', [])
                                         if str(eid) in eligible_ext_ids]
                        if not group_ext_ids:
                            continue
                        try:
                            # One at a time, in the order APS sent them. Assigning a
                            # whole group at once lets Odoo hand the scarce lot out in
                            # its own id order, so within a priority group the earlier
                            # due date could lose the stock to a later one.
                            for eid in group_ext_ids:
                                mo = mo_by_id.get(eid)
                                if not mo:
                                    continue
                                mo.action_assign()
                                summary['reReserved'] += 1
                                res_state_after = mo.reservation_state if hasattr(mo, 'reservation_state') else 'unknown'
                                reservation_status.append({
                                    'orderExternalId': str(mo.id),
                                    'orderNumber': mo.name,
                                    'action': 'RE_RESERVED',
                                    'reservationStateAfter': res_state_after,
                                })
                        except Exception as e:
                            group_priority = group.get('priority', '?')
                            summary['errors'] += len(group_ext_ids)
                            _logger.warning('Re-reserve error for priority group %s: %s', group_priority, str(e))
                            for eid in group_ext_ids:
                                mo = mo_by_id.get(eid)
                                reservation_status.append({
                                    'orderExternalId': eid,
                                    'orderNumber': mo.name if mo else 'unknown',
                                    'action': 'ERROR',
                                    'error': 'Re-reserve failed for priority group %s' % group_priority,
                                })
                elif eligible_ext_ids:
                    # Legacy flat format: re-reserve one by one in original order
                    error_ext_ids = {item['orderExternalId'] for item in reservation_status if item.get('action') == 'ERROR'}
                    for ext_id in all_ext_ids:
                        ext_id_str = str(ext_id)
                        if ext_id_str not in eligible_ext_ids or ext_id_str in error_ext_ids:
                            continue
                        mo = mo_by_id.get(ext_id_str)
                        if not mo:
                            continue
                        try:
                            mo.action_assign()
                            summary['reReserved'] += 1
                            res_state_after = mo.reservation_state if hasattr(mo, 'reservation_state') else 'unknown'
                            reservation_status.append({
                                'orderExternalId': ext_id_str,
                                'orderNumber': mo.name,
                                'action': 'RE_RESERVED',
                                'reservationStateAfter': res_state_after,
                            })
                        except Exception as e:
                            summary['errors'] += 1
                            _logger.warning('Re-reserve error for MO %s: %s', ext_id_str, str(e))
                            reservation_status.append({
                                'orderExternalId': ext_id_str,
                                'orderNumber': mo.name,
                                'action': 'ERROR',
                                'error': 'Re-reserve failed',
                            })
            else:
                # Dry run: report what would happen
                for ext_id_str in eligible_ext_ids:
                    mo = mo_by_id.get(ext_id_str)
                    if mo:
                        res_state = mo.reservation_state if hasattr(mo, 'reservation_state') else 'unknown'
                        reservation_status.append({
                            'orderExternalId': ext_id_str,
                            'orderNumber': mo.name,
                            'action': 'DRY_RUN',
                            'reservationStateBefore': res_state,
                        })

            return {
                'success': summary['errors'] == 0,
                'dryRun': dry_run,
                'freezeHorizonDays': freeze_horizon_days,
                'freezeCutoff': freeze_cutoff.isoformat() + 'Z',
                'summary': summary,
                'frozenOrders': frozen_orders,
                'reservationStatus': reservation_status,
            }

        except Exception as e:
            _logger.exception('Error re-reserving materials')
            return {'error': 'Internal error'}

