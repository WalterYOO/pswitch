package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSplitHostPort(t *testing.T) {
	host, port, err := SplitHostPort("127.0.0.1:17890")
	if err != nil || host != "127.0.0.1" || port != 17890 {
		t.Fatalf("got %q %d %v", host, port, err)
	}
	for _, bad := range []string{"", "host", ":80", "host:x", "host:99999"} {
		if _, _, err := SplitHostPort(bad); err == nil {
			t.Fatalf("%q should be rejected", bad)
		}
	}
}

func TestSplitHostPortDefault(t *testing.T) {
	host, port, err := SplitHostPortDefault("[::1]:8080", nil)
	if err != nil || host != "::1" || port != 8080 {
		t.Fatalf("ipv6: got %q %d %v", host, port, err)
	}
	def := 80
	host, port, err = SplitHostPortDefault("example.com", &def)
	if err != nil || host != "example.com" || port != 80 {
		t.Fatalf("default: got %q %d %v", host, port, err)
	}
	if _, _, err := SplitHostPortDefault("example.com", nil); err == nil {
		t.Fatal("missing port should be rejected without default")
	}
}

func TestConfigDefaultsAndExtra(t *testing.T) {
	home := t.TempDir()
	t.Setenv("PSWITCH_HOME", home)
	payload := `{
	  "mode": "manual",
	  "active": "a",
	  "custom_key": 123,
	  "upstreams": [
	    {"name": "a", "addr": "127.0.0.1:1"},
	    {"name": "b", "addr": "127.0.0.1:2", "enabled": false},
	    {"name": "", "addr": "127.0.0.1:3"}
	  ]
	}`
	path := filepath.Join(home, "config.json")
	if err := os.WriteFile(path, []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}

	c := Load()
	if c.Listen != "127.0.0.1:17000" || c.Mode != ModeManual {
		t.Fatalf("defaults not applied: %+v", c)
	}
	if len(c.Upstreams) != 2 {
		t.Fatalf("empty-name upstream should be filtered, got %d", len(c.Upstreams))
	}
	if !c.Upstreams[0].Enabled {
		t.Fatal("missing enabled should default to true")
	}
	if c.Upstreams[1].Enabled {
		t.Fatal("explicit enabled=false should be kept")
	}
	if got := len(Enabled(&c)); got != 1 {
		t.Fatalf("enabled count = %d", got)
	}

	if err := Save(c); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if m["custom_key"] != float64(123) {
		t.Fatalf("unknown key not preserved: %v", m["custom_key"])
	}
}
