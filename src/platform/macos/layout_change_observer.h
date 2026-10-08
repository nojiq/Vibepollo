#pragma once

#include <chrono>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <utility>

namespace platf::macos_virtual_display {
  // CoreGraphics also reports moves caused by reconnecting displays. Seed a
  // fresh baseline after those operations; only stable, subsequent user moves
  // should replace the saved arrangement.
  class layout_change_observer_t {
  public:
    using clock = std::chrono::steady_clock;
    using positions_t = std::map<std::string, std::tuple<int, int, int, int>>;

    void topology_changed(clock::time_point now, std::optional<positions_t> expected = std::nullopt) {
      settle_until_ = now + std::chrono::seconds(2);
      baseline_ = std::move(expected);
      candidate_.reset();
    }

    bool observe(const positions_t &positions, clock::time_point now) {
      if (now < settle_until_) return false;
      if (!baseline_) {
        baseline_ = positions;
        return false;
      }
      if (*baseline_ == positions) {
        candidate_.reset();
        return false;
      }
      if (!candidate_ || *candidate_ != positions) {
        candidate_ = positions;
        candidate_since_ = now;
        return false;
      }
      if (now - candidate_since_ < std::chrono::milliseconds(500)) return false;
      baseline_ = positions;
      candidate_.reset();
      return true;
    }

  private:
    clock::time_point settle_until_ {};
    clock::time_point candidate_since_ {};
    std::optional<positions_t> baseline_;
    std::optional<positions_t> candidate_;
  };
}  // namespace platf::macos_virtual_display
