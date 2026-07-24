/**
 * Jest configuration for CDK construct assertion tests.
 *
 * Uses ts-jest so TypeScript test files can `import` CDK constructs
 * directly. CDK Template-based tests run synth in-process; they're fast
 * (no docker, no network) — the 60s default timeout is plenty.
 */
module.exports = {
    testEnvironment: "node",
    testMatch: ["**/*.test.ts"],
    testPathIgnorePatterns: ["/node_modules/", "/cdk.out/"],
    transform: {
        "^.+\\.tsx?$": [
            "ts-jest",
            {
                // Swallow the noisy TS 6133 "declared but never read"
                // so tests don't fail on unrelated hint-level findings
                // elsewhere in the repo.
                diagnostics: { ignoreCodes: [6133] },
            },
        ],
    },
};
