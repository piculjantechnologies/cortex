import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { apiFetch, errorMessage } from '../api/client';
import './ForgotPasswordScreen.css';

// The backend answers the same way whether or not the email has an account, so the
// screen shows one neutral message instead of revealing which addresses are registered.
const RESET_LINK_SENT_MESSAGE =
  'If that email belongs to an account with a password, a reset link has been sent to it.';

const ForgotPasswordScreen = () => {
  const [email, setEmail] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const handleForgotPassword = async (e) => {
    e.preventDefault();
    setMessage('');
    setError('');

    try {
      await apiFetch('/api/forgot-password', {
        method: 'POST',
        body: { email },
        credentials: 'omit',
      });
      setMessage(RESET_LINK_SENT_MESSAGE);
    } catch (err) {
      setError(errorMessage(err, 'An error occurred while sending the reset link'));
    }
  };

  return (
    <div className="forgot-password-screen">
      <div className="forgot-password-container">
        <h1>Forgot Password</h1>
        {message && <p className="success-message" role="status">{message}</p>}
        {error && <p className="error-message" role="alert">{error}</p>}
        <form onSubmit={handleForgotPassword}>
          <div className="form-group">
            <label htmlFor="forgot-email">Email</label>
            <input
              id="forgot-email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <button type="submit">Send Reset Link</button>
        </form>

        <p>
          <Link to="/login">Return to Log In</Link>
        </p>
      </div>
    </div>
  );
};

export default ForgotPasswordScreen;
