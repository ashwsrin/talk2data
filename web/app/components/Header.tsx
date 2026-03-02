'use client';

import { useState, useRef, useEffect } from 'react';
import { useNavigation } from '../contexts/NavigationContext';

export default function Header() {
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const userName = 'User'; // Default user name for now
  const { navigateToSettings } = useNavigation();

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsDropdownOpen(false);
      }
    }

    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, []);

  return (
    <header className="w-full h-14 bg-app-surface border-b border-app-border flex items-center justify-between px-6 z-50">
      {/* Logo Section */}
      <div className="flex items-center gap-2">
        <img
          src="/logo.png"
          alt="Talk2Data"
          className="h-9 w-9 object-contain"
        />
        <h1 className="text-2xl font-bold text-app-text">
          Talk2Data
        </h1>
      </div>

      {/* Actions Section */}
      <div className="flex items-center gap-3">
        {/* Settings Icon */}
        <button
          onClick={() => navigateToSettings()}
          className="p-2 rounded-full hover:bg-app-sidebar-item-hover transition-colors text-app-text-muted hover:text-app-text"
          title="Settings"
          aria-label="Settings"
        >
          <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </button>

        {/* User Section */}
        <div className="relative" ref={dropdownRef}>
          <button
            onClick={() => setIsDropdownOpen(!isDropdownOpen)}
            className="flex items-center space-x-3 px-4 py-2 rounded-lg hover:opacity-90 transition-colors"
            aria-label="User menu"
          >
            {/* User Icon */}
            <div className="w-10 h-10 rounded-full bg-app-accent flex items-center justify-center">
              <svg
                className="w-6 h-6 text-white"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                />
              </svg>
            </div>

            {/* User Name */}
            <span className="text-sm font-medium text-app-text hidden sm:block">
              {userName}
            </span>

            {/* Dropdown Arrow */}
            <svg
              className={`w-4 h-4 text-app-text-muted transition-transform ${isDropdownOpen ? 'rotate-180' : ''
                }`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 9l-7 7-7-7"
              />
            </svg>
          </button>

          {/* Dropdown Menu */}
          {isDropdownOpen && (
            <div className="absolute right-0 mt-2 w-[20.7rem] min-w-[20.7rem] bg-app-surface rounded-lg shadow-lg border border-app-border py-1 z-50">
              {/* User Name in Dropdown (for mobile) */}
              <div className="px-4 py-2 border-b border-app-border sm:hidden">
                <p className="text-sm font-medium text-app-text">
                  {userName}
                </p>
              </div>



              {/* Sign Out */}
              <button
                onClick={() => {
                  setIsDropdownOpen(false);
                  // TODO: Implement sign out functionality
                  console.log('Sign out clicked');
                }}
                className="w-full text-left px-4 py-2 text-sm text-app-text hover:opacity-90 transition-colors flex items-center space-x-2"
              >
                <svg
                  className="w-4 h-4"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"
                  />
                </svg>
                <span>Sign Out</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </header >
  );
}
