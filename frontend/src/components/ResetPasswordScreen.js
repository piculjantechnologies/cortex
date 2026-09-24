import React, { useContext, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';
import { apiFetch, errorMessage } from '../api/client';
import './ResetPasswordScreen.css';

const ResetPasswordScreen = () => {
  const navigate = useNavigate();
  const { token } = useParams();
  const { setIsAuthenticated } = useContext(AuthContext);

  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');

  const handleResetPassword = async (e) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    try {
      // The token travels in the body of a fixed route, never in the request path.
      await apiFetch('/api/reset-password', {
        method: 'POST',
        body: { token, password },
        credentials: 'omit',
      });
      // The reset ends every session of the account, including one open in this browser.
      setIsAuthenticated(false);
      navigate('/login', {
        state: { notice: 'Your password has been reset. You can now log in.' },
      });
    } catch (err) {
      setError(errorMessage(err, 'An error occurred during password reset'));
    }
  };

  return (
    <div className="reset-password-screen">
      <div className="reset-password-container">
        <h1>Reset Password</h1>
        {error && <p className="error-message" role="alert">{error}</p>}
        <form onSubmit={handleResetPassword}>
          <div className="form-group">
            <label htmlFor="password">New Password</label>
            <input
              id="password"
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
            <label htmlFor="confirm-password">Confirm New Password</label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              minLength={6}
              maxLength={128}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>
          <button type="submit">Reset Password</button>
        </form>

        <p>
          <Link to="/login">Back to log in</Link>
        </p>
      </div>
    </div>
  );
};

export default ResetPasswordScreen;
