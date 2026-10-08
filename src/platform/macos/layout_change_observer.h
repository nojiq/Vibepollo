#pragma once

#include <algorithm>
#include <chrono>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

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
      last_change_included_physical_ = false;
    }

    void display_removed(const std::string &uuid, clock::time_point now) {
      settle_until_ = now + std::chrono::seconds(2);
      if (baseline_) baseline_->erase(uuid);
      candidate_.reset();
      last_change_included_physical_ = false;
    }

    bool observe(const positions_t &positions, clock::time_point now) {
      return observe_impl(positions, now, nullptr);
    }

    bool observe(const positions_t &positions, clock::time_point now, const std::vector<std::string> &physical_ids) {
      return observe_impl(positions, now, &physical_ids);
    }

    bool last_change_included_physical() const { return last_change_included_physical_; }

  private:
    static bool contains(const std::vector<std::string> *ids, const std::string &id) {
      return !ids || std::find(ids->begin(), ids->end(), id) != ids->end();
    }

    bool observe_impl(const positions_t &positions, clock::time_point now, const std::vector<std::string> *physical_ids) {
      last_change_included_physical_ = false;
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
      if (physical_ids) {
        last_change_included_physical_ = std::any_of(
          positions.begin(),
          positions.end(),
          [&](const auto &entry) {
            if (!contains(physical_ids, entry.first)) return false;
            const auto previous = baseline_->find(entry.first);
            return previous == baseline_->end() || previous->second != entry.second;
          }
        );
      } else {
        last_change_included_physical_ = true;
      }
      baseline_ = positions;
      candidate_.reset();
      return true;
    }

    clock::time_point settle_until_ {};
    clock::time_point candidate_since_ {};
    std::optional<positions_t> baseline_;
    std::optional<positions_t> candidate_;
    bool last_change_included_physical_ = false;
  };
}  // namespace platf::macos_virtual_display
