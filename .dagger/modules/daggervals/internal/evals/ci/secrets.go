package ci

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

type secretRule struct {
	name    string
	pattern *regexp.Regexp
}

// secretRules are common credential shapes. The scan is best-effort: a key split across
// lines or encoded (base64, escapes) gets through, which is why the exact values of the eval
// secrets are searched too where they are known (ci-eval-run, ci-eval-report).
var secretRules = []secretRule{
	{"sk- API key", regexp.MustCompile(`\bsk-[A-Za-z0-9_-]{20,}`)},
	{"GitHub token", regexp.MustCompile(`\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}`)},
	{"GitHub fine-grained token", regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{30,}`)},
	{"GitLab token", regexp.MustCompile(`\bglpat-[A-Za-z0-9_-]{20,}`)},
	{"Google API key", regexp.MustCompile(`\bAIza[0-9A-Za-z_-]{35}`)},
	{"Hugging Face token", regexp.MustCompile(`\bhf_[A-Za-z0-9]{30,}`)},
	{"npm token", regexp.MustCompile(`\bnpm_[A-Za-z0-9]{36}`)},
	{"AWS access key id", regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`)},
	{"Bearer or Basic credentials", regexp.MustCompile(`\b(?:Bearer|Basic) +[A-Za-z0-9._~+/=-]{20,}`)},
	{"credential assignment", regexp.MustCompile(`(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\\?["']?\s*[:=]\s*\\?["']?[A-Za-z0-9._~+/=-]{20,}`)},
	{"private key", regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`)},
	{"Slack token", regexp.MustCompile(`\bxox[a-z]-[A-Za-z0-9-]{10,}`)},
	{"JWT", regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`)},
}

// minSecretValueLength is the shortest secret value that is searched for.
const minSecretValueLength = 8

// secretEnvName are the env variables whose values are secrets; the others (a model name, a
// base URL) also show up in the agent's output and would fail every run.
var secretEnvName = regexp.MustCompile(`(?i)key|token|secret|passw|auth|credential`)

// EnvVar is one variable of an env file.
type EnvVar struct{ Name, Value string }

// SecretValues are the values to search for: those of the secret-named env variables and the
// string values of auth.json (the whole text when it is not JSON).
func SecretValues(env []EnvVar, authJSON string) []string {
	var values []string
	for _, v := range env {
		if secretEnvName.MatchString(v.Name) {
			values = append(values, v.Value)
		}
	}
	var auth any
	if err := json.Unmarshal([]byte(authJSON), &auth); err != nil {
		if s := strings.TrimSpace(authJSON); s != "" {
			values = append(values, s)
		}
		return values
	}
	var walk func(any)
	walk = func(v any) {
		switch v := v.(type) {
		case string:
			values = append(values, v)
		case map[string]any:
			for _, child := range v {
				walk(child)
			}
		case []any:
			for _, child := range v {
				walk(child)
			}
		}
	}
	walk(auth)
	return values
}

// SecretHits lists "file: rule" for every file that matches a secret shape or holds one of
// secretValues (as written or JSON-escaped). The matching text is never part of it.
func SecretHits(files map[string]string, secretValues []string) []string {
	var needles []string
	for _, v := range secretValues {
		if len(v) < minSecretValueLength {
			continue
		}
		escaped, _ := json.Marshal(v)
		needles = append(needles, v, strings.Trim(string(escaped), `"`))
	}
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	var hits []string
	for _, name := range names {
		content := files[name]
		for _, rule := range secretRules {
			if rule.pattern.MatchString(content) {
				hits = append(hits, name+": "+rule.name)
			}
		}
		for _, needle := range needles {
			if strings.Contains(content, needle) {
				hits = append(hits, name+": a value of the eval secrets")
				break
			}
		}
	}
	return hits
}

// ScanSecrets is SecretHits as an error.
func ScanSecrets(files map[string]string, secretValues []string) error {
	if hits := SecretHits(files, secretValues); len(hits) > 0 {
		return fmt.Errorf("secret scan: possible secrets in %s", strings.Join(hits, "; "))
	}
	return nil
}
