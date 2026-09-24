import React, { useContext, useState } from 'react';
import { GoogleLogin } from '@react-oauth/google';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';
import { apiFetch, errorMessage } from '../api/client';
import Footer from './Footer';
import './LoginScreen.css';

const LoginScreen = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { setIsAuthenticated } = useContext(AuthContext);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  // Set by the signup and reset-password screens when they send the user here.
  const notice = location.state && location.state.notice;

  const logIn = async (path, body, fallbackMessage) => {
    setError('');
    try {
      await apiFetch(path, { method: 'POST', body });
      setIsAuthenticated(true);
      navigate('/dashboard');
    } catch (err) {
      setError(errorMessage(err, fallbackMessage));
    }
  };

  const handleLogin = (e) => {
    e.preventDefault();
    logIn('/api/login', { email, password }, 'An error occurred during login');
  };

  // Google Identity Services returns a signed ID token; the backend verifies it.
  const handleGoogleSuccess = ({ credential }) => {
    logIn('/api/authorize/google', { credential }, 'Google login failed');
  };

  return (
    <div className="login-screen">
      <div className="login-container">
        <img src={`${process.env.PUBLIC_URL}/logo512.png`} alt="Cortex Logo" className="login-logo" />
        <h1>Cortex</h1>
        <h2>Web image scraper for object detection</h2>

        {notice && <p className="success-message" role="status">{notice}</p>}
        {error && <p className="error-message" role="alert">{error}</p>}

        <form onSubmit={handleLogin}>
          <div className="form-group">
            <label htmlFor="login-email">Email</label>
            <input
              id="login-email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit">Log in</button>
        </form>

        <div className="google-login-button">
          <GoogleLogin
            onSuccess={handleGoogleSuccess}
            onError={() => setError('Google login failed')}
            text="signin_with"
            width={320}
          />
        </div>

        <div className="additional-links">
          <p>Don't have an account? <Link to="/signup">Sign up</Link></p>
          <p><Link to="/forgot-password">Forgot your password?</Link></p>
        </div>
        <Footer />
      </div>
    </div>
  );
};

export default LoginScreen;
