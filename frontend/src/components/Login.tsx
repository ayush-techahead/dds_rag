import React, { useState } from 'react';
import { loginWithJson } from '../api/login';
import './Login.css';

export interface LoggedInUser {
  id?: string;
  email: string;
  full_name?: string | null;
}

interface LoginProps {
  onLoginSuccess: (
    accessToken: string,
    accountEmail: string,
    user: LoggedInUser
  ) => void | Promise<void>;
}

export const Login: React.FC<LoginProps> = ({ onLoginSuccess }) => {
  const [email, setemail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const result = await loginWithJson(email, password);
      if (!result.ok) {
        setError(result.message);
        return;
      }

      const accountEmail = email.trim();
      await onLoginSuccess(result.access_token, accountEmail, {
        email: accountEmail,
      });
    } catch (err) {
      console.error('Login error:', err);
      setError('An unexpected error occurred.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-container">
      <div className="login-card">
        <h2 className="login-title">DDS Demo Bot</h2>
        <p className="login-subtitle">Sign in to start your journey</p>

        <form onSubmit={handleSubmit} className="login-form">
          <div className="form-group">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setemail(e.target.value)}
              placeholder="Enter your email"
              required
              disabled={loading}
            />
          </div>

          <div className="form-group">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter your password"
              required
              minLength={1}
              maxLength={72}
              disabled={loading}
            />
          </div>

          {error && <div className="login-error">{error}</div>}

          <button type="submit" className="login-btn" disabled={loading}>
            {loading ? 'Signing In...' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
};
