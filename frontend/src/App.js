import React, { useContext } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { GoogleOAuthProvider } from '@react-oauth/google';
import { AuthContext, AuthProvider } from './context/AuthContext';
import LoginScreen from './components/LoginScreen';
import SignupScreen from './components/SignupScreen';
import ForgotPasswordScreen from './components/ForgotPasswordScreen';
import ResetPasswordScreen from './components/ResetPasswordScreen';
import Dashboard from './components/Dashboard';
import Spinner from './components/Spinner';

function AppRoutes() {
  const { isAuthenticated, checking } = useContext(AuthContext);

  if (checking) {
    return <Spinner label="Loading" />;
  }

  return (
    <Routes>
      <Route path="/" element={<Navigate to={isAuthenticated ? '/dashboard' : '/login'} replace />} />
      <Route
        path="/login"
        element={isAuthenticated ? <Navigate to="/dashboard" replace /> : <LoginScreen />}
      />
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="/signup" element={<SignupScreen />} />
      <Route path="/forgot-password" element={<ForgotPasswordScreen />} />
      <Route path="/reset-password/:token" element={<ResetPasswordScreen />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function App() {
  return (
    <GoogleOAuthProvider clientId={process.env.REACT_APP_GOOGLE_CLIENT_ID || ''}>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </GoogleOAuthProvider>
  );
}

export default App;
