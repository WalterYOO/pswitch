package cli

import (
	"testing"

	"pswitch/internal/config"
)

func TestResolveSystemTarget(t *testing.T) {
	cfg := config.Config{Upstreams: []config.Upstream{
		{Name: "office", Addr: "10.0.0.8:8080", Enabled: true},
		{Name: "off", Addr: "10.0.0.9:9090", Enabled: false},
	}}

	tests := []struct {
		name   string
		target string
		wantH  string
		wantP  int
		wantOK bool
	}{
		{"by name", "office", "10.0.0.8", 8080, true},
		{"by configured addr", "10.0.0.8:8080", "10.0.0.8", 8080, true},
		{"raw addr", "192.168.1.2:3128", "192.168.1.2", 3128, true},
		{"disabled upstream", "off", "", 0, false},
		{"unknown name", "nope", "", 0, false},
		{"bad addr", "nope:port", "", 0, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			h, p, ok := resolveSystemTarget(cfg, tt.target)
			if ok != tt.wantOK || h != tt.wantH || p != tt.wantP {
				t.Fatalf("resolveSystemTarget(%q) = (%q, %d, %v), want (%q, %d, %v)",
					tt.target, h, p, ok, tt.wantH, tt.wantP, tt.wantOK)
			}
		})
	}
}
