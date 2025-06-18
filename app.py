# app/__init__.py
import os
import uuid
import requests
import random

from flask import Flask, render_template, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from .models import db, User, UserMovie, Recommendation, Watchlist

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SESSION_SECRET", "default_secret_key")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    db.init_app(app)

    with app.app_context():
        db.create_all()
        register_routes(app)

    return app

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "a4747b23774690ec1831568f642ff364")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

def get_or_create_user():
    if 'user_session_id' not in session:
        session['user_session_id'] = str(uuid.uuid4())
    user = User.query.filter_by(session_id=session['user_session_id']).first()
    if not user:
        user = User(session_id=session['user_session_id'])
        db.session.add(user)
        db.session.commit()
    return user

def register_routes(app):
    @app.route('/')
    def index():
        return render_template('index.html', has_api_key=bool(TMDB_API_KEY))

    @app.route('/watchlist')
    def watchlist_page():
        return render_template('watchlist.html')

    @app.route('/api/search-movie', methods=['POST'])
    def search_movie():
        if not TMDB_API_KEY:
            return jsonify({'error': 'TMDB API key not configured'}), 500
        data = request.get_json()
        title = data.get('title', '').strip()
        if not title:
            return jsonify({'error': 'Movie title is required'}), 400

        try:
            response = requests.get(
                f"{TMDB_BASE_URL}/search/movie",
                params={
                    'api_key': TMDB_API_KEY,
                    'query': title,
                    'include_adult': False
                }, timeout=10)
            if response.status_code == 401:
                return jsonify({'error': 'Invalid TMDB API key'}), 401

            response.raise_for_status()
            movie_data = response.json()
            if movie_data.get('results'):
                movie = movie_data['results'][0]
                user = get_or_create_user()
                exists = UserMovie.query.filter_by(user_id=user.id, tmdb_id=movie['id']).first()
                if not exists:
                    user_movie = UserMovie(
                        user_id=user.id,
                        tmdb_id=movie['id'],
                        title=movie.get('title', ''),
                        release_date=movie.get('release_date', ''),
                        poster_path=movie.get('poster_path', ''),
                        overview=movie.get('overview', ''),
                        vote_average=movie.get('vote_average', 0),
                        genre_ids=movie.get('genre_ids', [])
                    )
                    db.session.add(user_movie)
                    db.session.commit()
                return jsonify({'movie': movie})
            return jsonify({'error': f'Movie "{title}" not found'}), 404
        except requests.exceptions.RequestException as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/get-recommendation', methods=['POST'])
    def get_recommendation():
        if not TMDB_API_KEY:
            return jsonify({'error': 'TMDB API key not configured'}), 500
        data = request.get_json()
        user_movies = data.get('movies', [])
        excluded_ids = data.get('excluded_ids', [])
        if len(user_movies) < 4:
            return jsonify({'error': 'Need at least 4 movies'}), 400

        try:
            user = get_or_create_user()
            genres = set()
            for m in user_movies:
                genres.update(m.get('genre_ids', []))
            if not genres:
                return jsonify({'error': 'No genres found'}), 400

            genre_str = ','.join(map(str, list(genres)[:3]))
            resp = requests.get(
                f"{TMDB_BASE_URL}/discover/movie",
                params={
                    'api_key': TMDB_API_KEY,
                    'with_genres': genre_str,
                    'sort_by': 'vote_average.desc',
                    'vote_count.gte': 100,
                    'include_adult': False
                }, timeout=10)
            resp.raise_for_status()
            results = [m for m in resp.json().get('results', []) if m['id'] not in excluded_ids and m.get('poster_path') and m.get('vote_average', 0) > 6]
            if not results:
                return jsonify({'error': 'No recommendations found'}), 404

            sorted_recs = sorted(results[:10], key=lambda x: x['vote_average'] * x.get('popularity', 0), reverse=True)
            recommendation = random.choice(sorted_recs[:5])
            details_resp = requests.get(f"{TMDB_BASE_URL}/movie/{recommendation['id']}", params={'api_key': TMDB_API_KEY}, timeout=10)
            genres_info = details_resp.json().get('genres', []) if details_resp.ok else []

            db.session.add(Recommendation(
                user_id=user.id,
                tmdb_id=recommendation['id'],
                title=recommendation.get('title', ''),
                release_date=recommendation.get('release_date', ''),
                poster_path=recommendation.get('poster_path', ''),
                overview=recommendation.get('overview', ''),
                vote_average=recommendation.get('vote_average', 0),
                genres=genres_info
            ))
            db.session.commit()
            return jsonify({'recommendation': recommendation})
        except requests.exceptions.RequestException as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/user-history')
    def get_user_history():
        try:
            user = get_or_create_user()
            user_movies = UserMovie.query.filter_by(user_id=user.id).order_by(UserMovie.added_at.desc()).all()
            recs = Recommendation.query.filter_by(user_id=user.id).order_by(Recommendation.recommended_at.desc()).all()
            return jsonify({
                'user_movies': [{
                    'tmdb_id': m.tmdb_id,
                    'title': m.title,
                    'poster_path': m.poster_path,
                    'added_at': m.added_at.isoformat()
                } for m in user_movies],
                'recommendations': [{
                    'tmdb_id': r.tmdb_id,
                    'title': r.title,
                    'poster_path': r.poster_path,
                    'vote_average': r.vote_average,
                    'recommended_at': r.recommended_at.isoformat(),
                    'was_liked': r.was_liked
                } for r in recs]
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/recommendation-feedback', methods=['POST'])
    def recommendation_feedback():
        try:
            data = request.get_json()
            rec_id = data.get('recommendation_id')
            liked = data.get('liked')
            user = get_or_create_user()
            rec = Recommendation.query.filter_by(tmdb_id=rec_id, user_id=user.id).first()
            if not rec:
                return jsonify({'error': 'Recommendation not found'}), 404
            rec.was_liked = liked
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/movie-suggestions', methods=['POST'])
    def get_movie_suggestions():
        if not TMDB_API_KEY:
            return jsonify({'error': 'TMDB API key not configured'}), 500
        data = request.get_json()
        query = data.get('query', '').strip()
        if len(query) < 3:
            return jsonify({'movies': []})
        try:
            r = requests.get(f"{TMDB_BASE_URL}/search/movie", params={'api_key': TMDB_API_KEY, 'query': query, 'include_adult': False}, timeout=10)
            r.raise_for_status()
            return jsonify({'movies': r.json().get('results', [])[:5]})
        except requests.exceptions.RequestException as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/add-to-watchlist', methods=['POST'])
    def add_to_watchlist():
        try:
            data = request.get_json()
            movie_id, title = data.get('movie_id'), data.get('title')
            if not movie_id or not title:
                return jsonify({'error': 'Movie ID and title are required'}), 400

            user = get_or_create_user()
            exists = Watchlist.query.filter_by(user_id=user.id, tmdb_id=movie_id).first()
            if exists:
                return jsonify({'error': 'Movie already in watchlist'}), 400

            r = requests.get(f"{TMDB_BASE_URL}/movie/{movie_id}", params={'api_key': TMDB_API_KEY}, timeout=10)
            if r.ok:
                movie_data = r.json()
                watchlist_item = Watchlist(
                    user_id=user.id,
                    tmdb_id=movie_id,
                    title=movie_data.get('title', title),
                    release_date=movie_data.get('release_date', ''),
                    poster_path=movie_data.get('poster_path', ''),
                    overview=movie_data.get('overview', ''),
                    vote_average=movie_data.get('vote_average', 0),
                    genres=movie_data.get('genres', [])
                )
                db.session.add(watchlist_item)
                db.session.commit()
                return jsonify({'success': True})
            return jsonify({'error': 'Failed to fetch movie details'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/watchlist')
    def get_watchlist():
        try:
            user = get_or_create_user()
            items = Watchlist.query.filter_by(user_id=user.id).order_by(Watchlist.added_at.desc()).all()
            return jsonify({'watchlist': [{
                'tmdb_id': i.tmdb_id,
                'title': i.title,
                'poster_path': i.poster_path,
                'vote_average': i.vote_average,
                'release_date': i.release_date,
                'added_at': i.added_at.isoformat()
            } for i in items]})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/remove-from-watchlist', methods=['POST'])
    def remove_from_watchlist():
        try:
            data = request.get_json()
            movie_id = data.get('movie_id')
            if not movie_id:
                return jsonify({'error': 'Movie ID is required'}), 400
            user = get_or_create_user()
            item = Watchlist.query.filter_by(user_id=user.id, tmdb_id=movie_id).first()
            if not item:
                return jsonify({'error': 'Movie not found in watchlist'}), 404
            db.session.delete(item)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
