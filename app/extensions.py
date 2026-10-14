from flask import jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
jwt = JWTManager()
cors = CORS()
csrf = CSRFProtect()


@jwt.invalid_token_loader
def handle_invalid_token(error_message: str):
    return jsonify({
        "message": "Die Anmeldung ist nicht mehr gültig. Bitte erneut anmelden."
    }), 401