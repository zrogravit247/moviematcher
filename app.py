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

def analyze_user_preferences(user_movies):
    """Analyze user movie preferences to understand their taste"""
    from collections import Counter
    import statistics
    
    # Extract all genres and count frequency
    all_genres = []
    ratings = []
    years = []
    
    for movie in user_movies:
        if movie.get('genre_ids'):
            all_genres.extend(movie['genre_ids'])
        if movie.get('vote_average'):
            ratings.append(movie['vote_average'])
        if movie.get('release_date'):
            try:
                year = int(movie['release_date'][:4])
                years.append(year)
            except (ValueError, IndexError):
                pass
    
    # Genre preferences (weighted by frequency)
    genre_counts = Counter(all_genres)
    primary_genres = [genre for genre, count in genre_counts.most_common(5)]
    
    # Rating preferences
    avg_rating = statistics.mean(ratings) if ratings else 7.0
    min_rating = max(6.0, avg_rating - 1.5)  # Don't go too low
    
    # Era preferences (with some flexibility)
    if years:
        avg_year = statistics.mean(years)
        year_range = max(15, statistics.stdev(years) * 2) if len(years) > 1 else 20
        era_start = max(1990, int(avg_year - year_range))
        era_end = min(2024, int(avg_year + year_range))
    else:
        era_start = 2000
        era_end = 2024
    
    return {
        'genres': primary_genres,
        'primary_genres': primary_genres[:3],
        'avg_rating': avg_rating,
        'min_rating': min_rating,
        'era_start': f"{era_start}-01-01",
        'era_end': f"{era_end}-12-31",
        'preferred_year_range': (era_start, era_end)
    }

def calculate_similarity_score(candidate, user_analysis, user):
    """Fast similarity scoring for candidate movies"""
    score = 0
    
    # Core genre matching (primary factor)
    candidate_genres = set(candidate.get('genre_ids', []))
    user_genres = set(user_analysis['primary_genres'])
    genre_overlap = len(candidate_genres.intersection(user_genres))
    
    if genre_overlap > 0:
        score += genre_overlap * 30
    
    # Quick user feedback check (cached for speed)
    if hasattr(user, '_genre_preferences_cache'):
        user_feedback = user._genre_preferences_cache
    else:
        user_feedback = get_user_genre_preferences(user)
        user._genre_preferences_cache = user_feedback
    
    for genre_id in candidate_genres:
        if genre_id in user_feedback['liked_genres']:
            score += 12
        elif genre_id in user_feedback['disliked_genres']:
            score -= 8
    
    # Rating quality
    candidate_rating = candidate.get('vote_average', 0)
    if candidate_rating >= 7.5:
        score += 10
    elif candidate_rating >= 7.0:
        score += 6
    elif candidate_rating < 6.0:
        score -= 10
    
    # Simple popularity balance
    popularity = candidate.get('popularity', 0)
    if 20 <= popularity <= 150:
        score += 8
    elif popularity > 300:
        score -= 2  # Avoid overly mainstream
    
    return max(0, score)

def get_user_genre_preferences(user):
    """Fast user genre preference lookup with caching"""
    liked_genres = set()
    disliked_genres = set()
    
    # Only check recent recommendations for speed
    recent_recs = models.Recommendation.query.filter_by(
        user_id=user.id
    ).filter(
        models.Recommendation.was_liked.isnot(None)
    ).order_by(
        models.Recommendation.recommended_at.desc()
    ).limit(20).all()
    
    for rec in recent_recs:
        if rec.genres:
            genre_ids = [g['id'] for g in rec.genres if 'id' in g]
            if rec.was_liked:
                liked_genres.update(genre_ids)
            else:
                disliked_genres.update(genre_ids)
    
    # Remove conflicts
    disliked_genres = disliked_genres - liked_genres
    
    return {
        'liked_genres': liked_genres,
        'disliked_genres': disliked_genres
    }

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
        
        # Analyze user preferences
        user_analysis = analyze_user_preferences(user_movies)
        
        if not user_analysis['genres']:
            return jsonify({'error': 'No genres found in provided movies'}), 400
        
        # Get candidates more efficiently with parallel requests
        import concurrent.futures
        import threading
        
        all_candidates = []
        primary_genres = user_analysis['primary_genres'][:2]
        
        def fetch_discover_movies(genres, sort_by='vote_average.desc', page=1):
            try:
                # Cache key for this request
                cache_key = f"discover_{','.join(map(str, genres))}_{sort_by}_{page}"
                
                response = requests.get(
                    f"{TMDB_BASE_URL}/discover/movie",
                    params={
                        'api_key': TMDB_API_KEY,
                        'with_genres': ','.join(map(str, genres)) if isinstance(genres, list) else str(genres),
                        'sort_by': sort_by,
                        'vote_count.gte': 20,  # Even lower for more variety
                        'vote_average.gte': 6.0,  # Fixed minimum quality
                        'include_adult': False,
                        'page': page
                    },
                    timeout=2  # Very fast timeout
                )
                return response.json().get('results', [])[:40] if response.ok else []
            except Exception as e:
                print(f"Discovery request failed: {e}")
                return []
        
        def fetch_similar_movies(movie_id):
            try:
                response = requests.get(
                    f"{TMDB_BASE_URL}/movie/{movie_id}/similar",
                    params={'api_key': TMDB_API_KEY, 'page': 1},
                    timeout=2
                )
                return response.json().get('results', [])[:15] if response.ok else []
            except Exception as e:
                print(f"Similar movies request failed: {e}")
                return []
        
        # Optimized concurrent requests with caching
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = []
            
            # Single high-quality request for primary genres
            futures.append(executor.submit(fetch_discover_movies, primary_genres, 'vote_average.desc'))
            
            # Similar movies only for highest-rated user movie
            if user_movies:
                best_movie = max(user_movies, key=lambda m: m.get('vote_average', 0))
                futures.append(executor.submit(fetch_similar_movies, best_movie['id']))
            
            # Collect results quickly
            for future in concurrent.futures.as_completed(futures, timeout=5):
                try:
                    results = future.result()
                    if results:
                        all_candidates.extend(results)
                except Exception as e:
                    print(f"Request failed: {e}")
                    continue
        
        # Fast deduplication and filtering
        seen_ids = set(excluded_ids)
        unique_candidates = []
        
        for movie in all_candidates:
            movie_id = movie.get('id')
            if (movie_id and movie_id not in seen_ids and
                movie.get('vote_average', 0) >= 6.0 and  # Quality threshold
                movie.get('poster_path') and
                movie.get('overview') and
                len(movie.get('overview', '')) > 20):  # Ensure meaningful overview
                seen_ids.add(movie_id)
                unique_candidates.append(movie)
                
                # Limit for performance while maintaining variety
                if len(unique_candidates) >= 60:
                    break
        
        if not unique_candidates:
            return jsonify({'error': 'No suitable recommendations found'}), 404
        
        # Quick scoring with simplified algorithm
        scored_candidates = []
        user_genres = set(user_analysis['primary_genres'])
        
        for candidate in unique_candidates:
            # Simplified fast scoring
            score = 0
            candidate_genres = set(candidate.get('genre_ids', []))
            genre_overlap = len(candidate_genres.intersection(user_genres))
            
            if genre_overlap > 0:
                score += genre_overlap * 30
            
            # Rating bonus
            rating = candidate.get('vote_average', 0)
            if rating >= 7.0:
                score += 15
            elif rating >= 6.5:
                score += 10
            
            # Popularity balance
            popularity = candidate.get('popularity', 0)
            if 10 <= popularity <= 200:
                score += 10
            elif popularity > 200:
                score += 5
            
            scored_candidates.append((candidate, score))
        
        # Selection from wider range of candidates for variety
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = scored_candidates[:20]  # Larger pool for variety
        
        import random
        # Simple weighted random selection
        if top_candidates:
            weights = [max(1, score) for _, score in top_candidates]
            recommendation = random.choices([movie for movie, _ in top_candidates], weights=weights)[0]
        else:
            # Fallback to first candidate if scoring fails
            recommendation = unique_candidates[0]
        
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

@app.route('/api/download-watchlist-csv')
def download_watchlist_csv():
    """Download user's watchlist as CSV"""
    try:
        user = get_or_create_user()
        watchlist_items = models.Watchlist.query.filter_by(user_id=user.id).order_by(models.Watchlist.added_at.desc()).all()
        
        # Create CSV content
        csv_content = "Title,Year,tmdbID\n"
        
        for item in watchlist_items:
            title = item.title.replace('"', '""')  # Escape quotes in CSV
            year = ""
            if item.release_date:
                try:
                    year = item.release_date[:4]
                except:
                    year = ""
            
            csv_content += f'"{title}",{year},{item.tmdb_id}\n'
        
        # Return as downloadable file
        from flask import Response
        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=my-watchlist.csv"}
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
