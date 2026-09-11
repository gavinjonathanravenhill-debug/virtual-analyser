from flask import Blueprint, jsonify, render_template, request
import logging

logger = logging.getLogger(__name__)

whale_bp = Blueprint('whale_tracker', __name__, url_prefix='/whale-tracker')

def init_whale_tracker_routes(app, tracker, db):
    @whale_bp.route('/', methods=['GET'])
    def whale_dashboard():
        return render_template('whale_tracker.html', whales={}, now=None)
    
    @whale_bp.route('/api/whale/<whale_addr>', methods=['GET'])
    def get_whale_positions(whale_addr):
        return jsonify({"wallet": whale_addr, "positions": []})
    
    app.register_blueprint(whale_bp)
    logger.info("Whale tracker routes registered")
