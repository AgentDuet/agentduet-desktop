import SwiftUI

/// The Google and Microsoft marks for the sign-in buttons (2026-09-29). Neither has an SF Symbol,
/// so they are drawn from the same shapes the HTML wizard's inline SVGs use — the providers'
/// own marks, in their own colours, which their sign-in guidelines ask for. Apple's is the SF
/// Symbol `apple.logo`.
struct GoogleMark: View {
    /// The official "G", on its 24-point grid: four pieces, one per colour.
    private static var parts: [(Color, Path)] { [
        // blue #4285F4
        (Color(red: 0.259, green: 0.522, blue: 0.957), Path { p in
            p.move(to: .init(x: 22.56, y: 12.25))
            p.addCurve(to: .init(x: 22.36, y: 10), control1: .init(x: 22.56, y: 11.47), control2: .init(x: 22.49, y: 10.72))
            p.addLine(to: .init(x: 12, y: 10))
            p.addLine(to: .init(x: 12, y: 14.26))
            p.addLine(to: .init(x: 17.92, y: 14.26))
            p.addCurve(to: .init(x: 15.71, y: 17.57), control1: .init(x: 17.66, y: 15.63), control2: .init(x: 16.88, y: 16.79))
            p.addLine(to: .init(x: 15.71, y: 20.34))
            p.addLine(to: .init(x: 19.28, y: 20.34))
            p.addCurve(to: .init(x: 22.56, y: 12.25), control1: .init(x: 21.36, y: 18.42), control2: .init(x: 22.56, y: 15.6))
            p.closeSubpath()
        }),
        // green #34A853
        (Color(red: 0.204, green: 0.659, blue: 0.325), Path { p in
            p.move(to: .init(x: 12, y: 23))
            p.addCurve(to: .init(x: 19.28, y: 20.34), control1: .init(x: 14.97, y: 23), control2: .init(x: 17.46, y: 22.02))
            p.addLine(to: .init(x: 15.71, y: 17.57))
            p.addCurve(to: .init(x: 12, y: 18.63), control1: .init(x: 14.73, y: 18.23), control2: .init(x: 13.48, y: 18.63))
            p.addCurve(to: .init(x: 5.84, y: 14.1), control1: .init(x: 9.14, y: 18.63), control2: .init(x: 6.71, y: 16.7))
            p.addLine(to: .init(x: 2.18, y: 14.1))
            p.addLine(to: .init(x: 2.18, y: 16.94))
            p.addCurve(to: .init(x: 12, y: 23), control1: .init(x: 3.99, y: 20.53), control2: .init(x: 7.7, y: 23))
            p.closeSubpath()
        }),
        // yellow #FBBC05
        (Color(red: 0.984, green: 0.737, blue: 0.020), Path { p in
            p.move(to: .init(x: 5.84, y: 14.09))
            p.addCurve(to: .init(x: 5.49, y: 12), control1: .init(x: 5.62, y: 13.43), control2: .init(x: 5.49, y: 12.73))
            p.addCurve(to: .init(x: 5.84, y: 9.91), control1: .init(x: 5.49, y: 11.27), control2: .init(x: 5.62, y: 10.57))
            p.addLine(to: .init(x: 5.84, y: 7.06))
            p.addLine(to: .init(x: 2.18, y: 7.06))
            p.addCurve(to: .init(x: 1, y: 12), control1: .init(x: 1.43, y: 8.55), control2: .init(x: 1, y: 10.22))
            p.addCurve(to: .init(x: 2.18, y: 16.94), control1: .init(x: 1, y: 13.78), control2: .init(x: 1.43, y: 15.45))
            p.addLine(to: .init(x: 5.03, y: 14.72))
            p.addLine(to: .init(x: 5.84, y: 14.09))
            p.closeSubpath()
        }),
        // red #EA4335
        (Color(red: 0.918, green: 0.263, blue: 0.208), Path { p in
            p.move(to: .init(x: 12, y: 5.38))
            p.addCurve(to: .init(x: 16.21, y: 7.02), control1: .init(x: 13.62, y: 5.38), control2: .init(x: 15.06, y: 5.94))
            p.addLine(to: .init(x: 19.36, y: 3.87))
            p.addCurve(to: .init(x: 12, y: 1), control1: .init(x: 17.45, y: 2.09), control2: .init(x: 14.97, y: 1))
            p.addCurve(to: .init(x: 2.18, y: 7.06), control1: .init(x: 7.7, y: 1), control2: .init(x: 3.99, y: 3.47))
            p.addLine(to: .init(x: 5.84, y: 9.9))
            p.addCurve(to: .init(x: 12, y: 5.38), control1: .init(x: 6.71, y: 7.3), control2: .init(x: 9.14, y: 5.38))
            p.closeSubpath()
        }),
    ] }

    var body: some View {
        Canvas { context, size in
            let scale = min(size.width, size.height) / 24
            context.scaleBy(x: scale, y: scale)
            for (color, path) in Self.parts { context.fill(path, with: .color(color)) }
        }
        .frame(width: 16, height: 16)
    }
}

struct MicrosoftMark: View {
    var body: some View {
        // Four squares on a 23-point grid, a point apart.
        Canvas { context, size in
            let s = min(size.width, size.height) / 23
            let squares: [(Double, Double, Color)] = [
                (1, 1, Color(red: 0.953, green: 0.325, blue: 0.145)),
                (12, 1, Color(red: 0.506, green: 0.737, blue: 0.024)),
                (1, 12, Color(red: 0.020, green: 0.651, blue: 0.941)),
                (12, 12, Color(red: 1.000, green: 0.729, blue: 0.031)),
            ]
            for (x, y, color) in squares {
                context.fill(Path(CGRect(x: x * s, y: y * s, width: 10 * s, height: 10 * s)),
                             with: .color(color))
            }
        }
        .frame(width: 15, height: 15)
    }
}
