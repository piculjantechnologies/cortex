import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { apiFetch, errorMessage } from '../api/client';
import './SignupScreen.css';

const SignupScreen = () => {
  const navigate = useNavigate();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');

  const handleSignup = async (e) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    try {
      await apiFetch('/api/register', {
        method: 'POST',
        body: { email, password },
        credentials: 'omit',
      });
      navigate('/login', { state: { notice: 'Signup successful! You can now log in.' } });
    } catch (err) {
      setError(errorMessage(err, 'An error occurred during signup'));
    }
  };

  return (
    <div className="signup-screen">
      <div className="signup-container">
        <h1>Sign Up</h1>
        {error && <p className="error-message" role="alert">{error}</p>}
        <form onSubmit={handleSignup}>
          <div className="form-group">
            <label htmlFor="signup-email">Email</label>
            <input
              id="signup-email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label htmlFor="signup-password">Password</label>
            <input
              id="signup-password"
              type="password"
              autoComplete="new-password"
              minLength={6}
              maxLength={128}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label htmlFor="signup-confirm-password">Confirm Password</label>
            <input
              id="signup-confirm-password"
              type="password"
              autoComplete="new-password"
              minLength={6}
              maxLength={128}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit">Sign Up</button>
        </form>

        <p>
          Already have an account? <Link to="/login">Log in here</Link>
        </p>
      </div>
    </div>
  );
};

export default SignupScreen;
