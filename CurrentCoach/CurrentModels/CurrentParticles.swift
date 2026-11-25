import Foundation

/// The moving streaks that show which way the water goes and how fast:
/// each one is dropped on the water at random, carried by the current for
/// a few seconds, and leaves a short tail. Positions are in grid
/// coordinates (columns from the west edge, rows from the south edge).
struct CurrentParticleSystem: Sendable {
    struct Point: Sendable, Equatable {
        var x: Double
        var y: Double
    }

    struct Particle: Sendable, Equatable {
        var trail: [Point]
        var age: Double
        var life: Double

        /// 0 when just dropped or about to go, 1 in the middle of its life.
        var strength: Double {
            guard life > 0 else { return 0 }
            let share = age / life
            return max(0, min(1, min(share / 0.15, (1 - share) / 0.3)))
        }
    }

    /// The part of the grid the streaks live in, usually what is on screen.
    struct Bounds: Sendable, Equatable {
        var minX: Double
        var maxX: Double
        var minY: Double
        var maxY: Double

        var isEmpty: Bool { !(maxX > minX && maxY > minY) }
    }

    static let trailLength = 14

    private(set) var particles: [Particle]
    private var random: SplitMix64

    init(count: Int, seed: UInt64 = 0x5EED) {
        random = SplitMix64(state: seed)
        particles = Array(repeating: Particle(trail: [], age: 0, life: 0), count: max(0, count))
    }

    mutating func resize(to count: Int) {
        let count = max(0, count)
        if count < particles.count {
            particles.removeLast(particles.count - count)
        } else if count > particles.count {
            particles.append(contentsOf: Array(repeating: Particle(trail: [], age: 0, life: 0), count: count - particles.count))
        }
    }

    /// Forget every streak, for when the map jumps somewhere else.
    mutating func reset() {
        for index in particles.indices {
            particles[index] = Particle(trail: [], age: 0, life: 0)
        }
    }

    /// Move every streak on by `dt` seconds of screen time. `cellsPerSecond`
    /// is how many grid cells a current of one metre per second covers in
    /// one second of screen time, across and up.
    mutating func step(frame: CurrentFrame, bounds: Bounds, dt: Double, cellsPerSecond: (x: Double, y: Double)) {
        guard !bounds.isEmpty, dt > 0 else { return }
        for index in particles.indices {
            var particle = particles[index]
            var alive = particle.age < particle.life
            if alive, let head = particle.trail.last, let velocity = frame.velocity(x: head.x, y: head.y) {
                let next = Point(
                    x: head.x + velocity.u * cellsPerSecond.x * dt,
                    y: head.y + velocity.v * cellsPerSecond.y * dt
                )
                if next.x < bounds.minX || next.x > bounds.maxX || next.y < bounds.minY || next.y > bounds.maxY {
                    alive = false
                } else {
                    particle.trail.append(next)
                    if particle.trail.count > Self.trailLength {
                        particle.trail.removeFirst(particle.trail.count - Self.trailLength)
                    }
                    particle.age += dt
                }
            } else {
                alive = false
            }
            if !alive {
                particle = drop(frame: frame, bounds: bounds)
            }
            particles[index] = particle
        }
    }

    /// A new streak somewhere on the water inside the bounds. If a few tries
    /// all land ashore it waits, out of sight, for the next step.
    private mutating func drop(frame: CurrentFrame, bounds: Bounds) -> Particle {
        for _ in 0..<6 {
            let point = Point(
                x: bounds.minX + random.unit() * (bounds.maxX - bounds.minX),
                y: bounds.minY + random.unit() * (bounds.maxY - bounds.minY)
            )
            if frame.velocity(x: point.x, y: point.y) != nil {
                return Particle(trail: [point], age: 0, life: 2.5 + 3.5 * random.unit())
            }
        }
        return Particle(trail: [], age: 0, life: 0)
    }
}

/// A small, repeatable random number generator.
struct SplitMix64: Sendable {
    var state: UInt64

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    /// A number from 0 up to, not including, 1.
    mutating func unit() -> Double {
        Double(next() >> 11) / Double(1 << 53)
    }
}
