import os
import requests
import uuid
from flask import Flask, render_template, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

# create the app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "default_secret_key")

# configure the database
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# initialize the app with the extension
db.init_app(app)

with app.app_context():
    # Import models after db is initialized
    import models
    db.create_all()

# TMDB API configuration
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "a4747b23774690ec1831568f642ff364")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

def get_or_create_user():
    """Get or create a user based on session"""
    if 'user_session_id' not in session:
        session['user_session_id'] = str(uuid.uuid4())
    
    user = models.User.query.filter_by(session_id=session['user_session_id']).first()
    if not user:
        user = models.User(session_id=session['user_session_id'])
        db.session.add(user)
        db.session.commit()
    
    return user

@app.route('/')
def index():
    # Check if API key is configured
    has_api_key = bool(TMDB_API_KEY)
    return render_template('index.html', has_api_key=has_api_key)

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
            },
            timeout=10
        )
        
        if response.status_code == 401:
            return jsonify({'error': 'Invalid TMDB API key'}), 401
        
        response.raise_for_status()
        data = response.json()
        
        if data.get('results'):
            movie = data['results'][0]
            
            # Save movie to user's preferences
            user = get_or_create_user()
            
            # Check if this movie is already in user's list
            existing_movie = models.UserMovie.query.filter_by(
                user_id=user.id,
                tmdb_id=movie['id']
            ).first()
            
            if not existing_movie:
                user_movie = models.UserMovie(
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
        else:
            return jsonify({'error': f'Movie "{title}" not found'}), 404
            
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to search for movie: {str(e)}'}), 500

@app.route('/api/get-recommendation', methods=['POST'])
def get_recommendation():
    if not TMDB_API_KEY:
        return jsonify({'error': 'TMDB API key not configured'}), 500
    
    data = request.get_json()
    user_movies = data.get('movies', [])
    excluded_ids = data.get('excluded_ids', [])
    
    if len(user_movies) < 4:
        return jsonify({'error': 'Need at least 4 movies for recommendation'}), 400
    
    try:
        user = get_or_create_user()
        
        # Extract genres from user movies
        all_genres = set()
        for movie in user_movies:
            if movie.get('genre_ids'):
                all_genres.update(movie['genre_ids'])
        
        if not all_genres:
            return jsonify({'error': 'No genres found in provided movies'}), 400
        
        # Search for movies with similar genres
        genre_list = ','.join(map(str, list(all_genres)[:3]))  # Use top 3 genres
        
        response = requests.get(
            f"{TMDB_BASE_URL}/discover/movie",
            params={
                'api_key': TMDB_API_KEY,
                'with_genres': genre_list,
                'sort_by': 'vote_average.desc',
                'vote_count.gte': 100,
                'include_adult': False,
                'page': 1
            },
            timeout=10
        )
        
        response.raise_for_status()
        recommendations = response.json().get('results', [])
        
        # Filter out user movies and previously recommended movies
        filtered_recommendations = [
            movie for movie in recommendations
            if movie['id'] not in excluded_ids and
               movie.get('vote_average', 0) > 6.0 and
               movie.get('poster_path')
        ]
        
        if not filtered_recommendations:
            return jsonify({'error': 'No suitable recommendations found'}), 404
        
        # Sort by rating and popularity, then pick randomly from top results
        sorted_recs = sorted(
            filtered_recommendations[:10],
            key=lambda x: x.get('vote_average', 0) * x.get('popularity', 0),
            reverse=True
        )
        
        import random
        recommendation = random.choice(sorted_recs[:5])  # Pick from top 5
        
        # Get detailed movie information for genres
        movie_details_response = requests.get(
            f"{TMDB_BASE_URL}/movie/{recommendation['id']}",
            params={'api_key': TMDB_API_KEY},
            timeout=10
        )
        
        if movie_details_response.ok:
            detailed_movie = movie_details_response.json()
            genres_info = detailed_movie.get('genres', [])
        else:
            genres_info = []
        
        # Save recommendation to database
        db_recommendation = models.Recommendation(
            user_id=user.id,
            tmdb_id=recommendation['id'],
            title=recommendation.get('title', ''),
            release_date=recommendation.get('release_date', ''),
            poster_path=recommendation.get('poster_path', ''),
            overview=recommendation.get('overview', ''),
            vote_average=recommendation.get('vote_average', 0),
            genres=genres_info
        )
        db.session.add(db_recommendation)
        db.session.commit()
        
        return jsonify({'recommendation': recommendation})
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to get recommendation: {str(e)}'}), 500

@app.route('/api/movie-details/<int:movie_id>')
def get_movie_details(movie_id):
    if not TMDB_API_KEY:
        return jsonify({'error': 'TMDB API key not configured'}), 500
    
    try:
        response = requests.get(
            f"{TMDB_BASE_URL}/movie/{movie_id}",
            params={'api_key': TMDB_API_KEY},
            timeout=10
        )
        
        response.raise_for_status()
        movie_details = response.json()
        
        return jsonify({'movie': movie_details})
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to get movie details: {str(e)}'}), 500

@app.route('/api/user-history')
def get_user_history():
    """Get user's movie preferences and recommendation history"""
    try:
        user = get_or_create_user()
        
        # Get user's favorite movies
        user_movies = models.UserMovie.query.filter_by(user_id=user.id).order_by(models.UserMovie.added_at.desc()).all()
        
        # Get user's recommendation history
        recommendations = models.Recommendation.query.filter_by(user_id=user.id).order_by(models.Recommendation.recommended_at.desc()).all()
        
        return jsonify({
            'user_movies': [{
                'tmdb_id': movie.tmdb_id,
                'title': movie.title,
                'poster_path': movie.poster_path,
                'added_at': movie.added_at.isoformat()
            } for movie in user_movies],
            'recommendations': [{
                'tmdb_id': rec.tmdb_id,
                'title': rec.title,
                'poster_path': rec.poster_path,
                'vote_average': rec.vote_average,
                'recommended_at': rec.recommended_at.isoformat(),
                'was_liked': rec.was_liked
            } for rec in recommendations]
        })
        
    except Exception as e:
        return jsonify({'error': f'Failed to get user history: {str(e)}'}), 500

@app.route('/api/recommendation-feedback', methods=['POST'])
def recommendation_feedback():
    """Allow users to provide feedback on recommendations"""
    try:
        data = request.get_json()
        recommendation_id = data.get('recommendation_id')
        liked = data.get('liked')  # True for liked, False for disliked
        
        user = get_or_create_user()
        
        recommendation = models.Recommendation.query.filter_by(
            tmdb_id=recommendation_id,
            user_id=user.id
        ).first()
        
        if not recommendation:
            return jsonify({'error': 'Recommendation not found'}), 404
        
        recommendation.was_liked = liked
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Feedback recorded'})
        
    except Exception as e:
        return jsonify({'error': f'Failed to record feedback: {str(e)}'}), 500

@app.route('/api/movie-suggestions', methods=['POST'])
def get_movie_suggestions():
    """Get movie suggestions for autocomplete"""
    if not TMDB_API_KEY:
        return jsonify({'error': 'TMDB API key not configured'}), 500
    
    data = request.get_json()
    query = data.get('query', '').strip()
    
    if len(query) < 3:
        return jsonify({'movies': []})
    
    try:
        response = requests.get(
            f"{TMDB_BASE_URL}/search/movie",
            params={
                'api_key': TMDB_API_KEY,
                'query': query,
                'include_adult': False,
                'page': 1
            },
            timeout=10
        )
        
        response.raise_for_status()
        data = response.json()
        
        movies = data.get('results', [])[:5]  # Limit to 5 suggestions
        
        return jsonify({'movies': movies})
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch suggestions: {str(e)}'}), 500

@app.route('/api/add-to-watchlist', methods=['POST'])
def add_to_watchlist():
    """Add a movie to user's watchlist"""
    try:
        data = request.get_json()
        movie_id = data.get('movie_id')
        title = data.get('title')
        
        if not movie_id or not title:
            return jsonify({'error': 'Movie ID and title are required'}), 400
        
        user = get_or_create_user()
        
        # Check if movie is already in watchlist
        existing_watchlist = models.Watchlist.query.filter_by(
            user_id=user.id,
            tmdb_id=movie_id
        ).first()
        
        if existing_watchlist:
            return jsonify({'error': 'Movie already in watchlist'}), 400
        
        # Get movie details from TMDB
        try:
            response = requests.get(
                f"{TMDB_BASE_URL}/movie/{movie_id}",
                params={'api_key': TMDB_API_KEY},
                timeout=10
            )
            
            if response.ok:
                movie_data = response.json()
                
                watchlist_item = models.Watchlist(
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
                
                return jsonify({'success': True, 'message': 'Movie added to watchlist'})
            else:
                return jsonify({'error': 'Failed to fetch movie details'}), 500
                
        except requests.exceptions.RequestException:
            return jsonify({'error': 'Failed to fetch movie details'}), 500
        
    except Exception as e:
        return jsonify({'error': f'Failed to add to watchlist: {str(e)}'}), 500

@app.route('/api/watchlist')
def get_watchlist():
    """Get user's watchlist"""
    try:
        user = get_or_create_user()
        
        watchlist_items = models.Watchlist.query.filter_by(user_id=user.id).order_by(models.Watchlist.added_at.desc()).all()
        
        return jsonify({
            'watchlist': [{
                'tmdb_id': item.tmdb_id,
                'title': item.title,
                'poster_path': item.poster_path,
                'vote_average': item.vote_average,
                'release_date': item.release_date,
                'added_at': item.added_at.isoformat()
            } for item in watchlist_items]
        })
        
    except Exception as e:
        return jsonify({'error': f'Failed to get watchlist: {str(e)}'}), 500

@app.route('/api/remove-from-watchlist', methods=['POST'])
def remove_from_watchlist():
    """Remove a movie from user's watchlist"""
    try:
        data = request.get_json()
        movie_id = data.get('movie_id')
        
        if not movie_id:
            return jsonify({'error': 'Movie ID is required'}), 400
        
        user = get_or_create_user()
        
        watchlist_item = models.Watchlist.query.filter_by(
            user_id=user.id,
            tmdb_id=movie_id
        ).first()
        
        if not watchlist_item:
            return jsonify({'error': 'Movie not found in watchlist'}), 404
        
        db.session.delete(watchlist_item)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Movie removed from watchlist'})
        
    except Exception as e:
        return jsonify({'error': f'Failed to remove from watchlist: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
