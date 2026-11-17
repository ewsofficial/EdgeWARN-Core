/**
 * Jest configuration for EdgeWARN API tests
 */

export default {
    // Keep nested checkouts/worktrees out of repository test discovery.
    roots: ['<rootDir>/tests/api'],
    // Test environment
    testEnvironment: 'node',

    // Coverage configuration
    coverageDirectory: 'coverage',
    collectCoverageFrom: [
        'src/api/**/*.js',
        'src/config/loader.js',
        '!src/api/**/*.test.js'
    ],
    coverageThreshold: {
        global: {
            branches: 66,
            functions: 78,
            lines: 89,
            statements: 70
        }
    },

    // Test file patterns
    testMatch: [
        '**/tests/api/**/*.test.js',
        '**/tests/api/**/*.spec.js',
        '**/tests/api/**/test_*.js'
    ],

    // Module handling for ES modules
    transform: {},
    moduleNameMapper: {
        '^(\\.{1,2}/.*)\\.js$': '$1'
    },

    // Setup files
    setupFilesAfterEnv: [],

    // Verbose output
    verbose: true,

    // Clear mocks between tests
    clearMocks: true,

    // Restore mocks after each test
    restoreMocks: true,

    // Test timeout (10 seconds)
    testTimeout: 10000
};
