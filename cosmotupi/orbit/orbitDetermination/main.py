import numpy as np
from scipy.optimize import least_squares, minimize, differential_evolution
import warnings
from coordinate_utils import convert_to_cartesian
from ephemeris_utils import get_earth_position_and_velocity, solar_system_perturbations
from orbital_mechanics import light_travel_correction, calculate_orbital_elements, estimate_angular_velocity
from propagation import propagate_orbit

# constantes 
MU_SUN = 0.000295912208  # AU^3/dia^2
C_LIGHT = 173.1446 # AU/dia


# --------------------------------------------------------------------------------------------------------
def differential_correction(R, v_earth, rho_hat, t, mu_sun, perturbations=None):
    """
    Applies differential correction to refine the initial estimate of the object's orbit

    Args:
        R (list): List of position vectors (earth)
        v_earth (list): List of velocity vectors (earth)
        rho_hat (list): List of unit direction vectors
        t (list): List of datetime objects
        mu_sun (float): Gravitational parameter of the Sun
    
    Returns:
        np.array: Refined position vector
        np.array: Refined velocity vector
    """
    r_initial, v_initial = gauss_iod_method(R, rho_hat, t)   
   
    if r_initial is None:
        best_result = None
        best_cost = float('inf')
        r_earth_sun = np.linalg.norm(R[0])
        bounds = [
            (-5.0, 5.0), (-5.0, 5.0), (-5.0, 5.0),
            (-0.07, 0.07), (-0.07, 0.07), (-0.07, 0.07)
        ]
        def objective(params):
            r0, v0 = params[:3], params[3:]
            r_norm = np.linalg.norm(r0)
            if r_norm < 0.1 or r_norm > 10.0:
                return 1e10
         
            try:
                r_pred, _ = propagate_orbit(r0, v0, t, t[0], mu_sun)
                cost = 0
                weights = np.linspace(1.0, 0.8, len(t))
            
                for i in range(len(t)):
                    rho_vec = r_pred[i] - R[i]
                    rho_norm = np.linalg.norm(rho_vec)
                    if rho_norm < 1e-10:
                        return 1e10
                    rho_pred = rho_vec / rho_norm
                    ang_error = np.arccos(np.clip(np.dot(rho_pred, rho_hat[i]), -1.0, 1.0))
                    cost += weights[i] * ang_error**2
                r_norm = np.linalg.norm(r0)
                v_norm = np.linalg.norm(v0)
                energy = v_norm**2 / 2 - mu_sun / r_norm
                h = np.cross(r0, v0)
                h_norm = np.linalg.norm(h)
                if h_norm > 1e-10:
                    e_vec = np.cross(v0, h) / mu_sun - r0 / r_norm
                e = np.linalg.norm(e_vec)
                if energy > 0:  
                    cost += 200 * energy  
                elif -mu_sun / (2 * energy) > 5.0: 
                    cost += 50 * (-mu_sun / (2 * energy) - 5.0)
                if e > 0.95: 
                    cost += 100 * (e - 0.95)**2
                return cost
            except Exception:
                return 1e10
        for trial in range(7): 
            r_scale = 0.7 + 0.3 * trial 
            v_scale = 0.008 + 0.008 * trial
            x0 = np.zeros(6)
            random_offset = np.random.uniform(-0.05, 0.05, 3) if trial > 0 else np.zeros(3)
            x0[:3] = R[0] + r_scale * (rho_hat[0] + random_offset)
            rho_hat_dot = estimate_angular_velocity(rho_hat, t)
            v_offset = np.random.uniform(-0.003, 0.003, 3) if trial > 0 else np.zeros(3)
            x0[3:] = v_earth[0] + v_scale * rho_hat[0] + r_scale * rho_hat_dot + v_offset
            try:
                methods = ['Nelder-Mead', 'Powell'] if trial < 2 else ['Nelder-Mead']
                for method in methods:
                    result = minimize(objective, x0, method=method, bounds=bounds, options={'maxiter': 1500})
                    if result.fun < best_cost:
                        best_cost = result.fun
                        best_result = result.x
            except:
                continue
        if best_result is not None:
            r_initial, v_initial = best_result[:3], best_result[3:]
        else:
            # Se tudo falhar -> tentar otimização global 
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                result = differential_evolution(objective, bounds, popsize=25, maxiter=40, strategy='best1bin', tol=1e-7)
                r_initial, v_initial = result.x[:3], result.x[3:]
            except:
                print("Falha na otimização global.")
                return None, None
    if r_initial is None:
        return None, None
   
    def residuals(params):
        r0, v0 = params[:3], params[3:]
        r_norm = np.linalg.norm(r0)
        v_norm = np.linalg.norm(v0)
        energy = 0.5 * v_norm**2 - mu_sun / r_norm
        h = np.cross(r0, v0)
        h_norm = np.linalg.norm(h)
        penalty = 0
        if h_norm > 1e-10:
            a = -mu_sun / (2 * energy) if energy < 0 else float('inf')
            e_vec = np.cross(v0, h) / mu_sun - r0 / r_norm
            e = np.linalg.norm(e_vec)
            if energy > 0:  # Órbita hiperbólica
                penalty += 500 * energy
            elif a < 0.3:  # Órbita muito pequena
                penalty += 100 * (0.3 - a)**2
            elif a > 5.0:  # Órbita muito grande
                penalty += 50 * (a - 5.0)**2
            if e > 0.98:  # Excentricidade muito alta
                penalty += 200 * (e - 0.98)**2
        else:
            penalty += 1000
        try:
            r_pred, _ = propagate_orbit(r0, v0, t, t[0], mu_sun, perturbations)
        except Exception as e:
            return np.ones(len(t) * 3) * 1e8 + penalty
        weights = np.linspace(1.2, 0.8, len(t))
        resid = []
        for i in range(len(t)):
            rho_vec = r_pred[i] - R[i]
            rho_norm = np.linalg.norm(rho_vec)
            if rho_norm < 1e-10:
                return np.ones(len(t) * 3) * 1e8 + penalty
            rho_pred = rho_vec / rho_norm
            resid.append((rho_pred - rho_hat[i]) * weights[i])
        flat_resid = np.concatenate(resid)
        if penalty > 0:
            flat_resid = flat_resid + penalty * np.ones_like(flat_resid) / len(flat_resid)
        return flat_resid
   
   # rfinamento final com least_squares usando bounds mais amplos
    initial_params = np.hstack((r_initial, v_initial))
    bounds = ([-15, -15, -15, -0.15, -0.15, -0.15], [15, 15, 15, 0.15, 0.15, 0.15])
   
    try:
        result = least_squares(residuals, initial_params, method='trf', bounds=bounds, ftol=1e-10, xtol=1e-10, gtol=1e-10, max_nfev=10000, verbose=0)
        r_final, v_final = result.x[:3], result.x[3:]
        r_norm = np.linalg.norm(r_final)
        v_norm = np.linalg.norm(v_final)
        energy = 0.5 * v_norm**2 - mu_sun / r_norm
        a = -mu_sun / (2 * energy) if energy < 0 else float('inf')
        h = np.cross(r_final, v_final)
        h_norm = np.linalg.norm(h)
        if h_norm > 1e-10:
            e_vec = np.cross(v_final, h) / mu_sun - r_final / r_norm
            e = np.linalg.norm(e_vec)
            i = np.degrees(np.arccos(abs(h[2]) / h_norm))
            if energy > 0: 
                print(f"Solução hiperbólica: energy={energy:.8f}, e={e:.6f}")
                if energy > 1e-4: 
                    return None, None
            elif a > 10.0:
                print(f"Solução com semi-eixo maior excessivo: a={a:.6f} AU")
                return None, None
            elif e > 0.99:
                print(f"Solução com excentricidade excessiva: e={e:.6f}")
                return None, None
            print(f"Solução aceita: a={a:.6f} AU, e={e:.6f}, i={i:.6f}°")
            return r_final, v_final
        else:
            print("Solução com momento angular quase nulo.")
            return None, None
    except Exception as e:
        print(f"Erro no refinamento: {e}")
        return None, None


# --------------------------------------------------------------------------------------------------------
def process_asteroid(name, observations, observatory=None):
   """
   Processa observações de um asteroide e determina sua órbita
   """
   print(f"\n### Calculando órbita de {name} usando {len(observations)} observaçoes ###")
   t = [obs["timestamp"] for obs in observations]
   rho_hat = [convert_to_cartesian(obs["RA"], obs["DEC"]) for obs in observations]
   R, v_earth = get_earth_position_and_velocity(t, observatory)
   rho_initial = np.ones(len(t))

   for iteration in range(10):
      print(f"Iteração {iteration + 1}:")
      t_corr = light_travel_correction(rho_initial, t)
      R, v_earth = get_earth_position_and_velocity(t_corr)
      def perturbation_model(t, r, v):
            return solar_system_perturbations(t, r, v, t_corr[0])
      r_final, v_final = differential_correction(R, v_earth, rho_hat, t_corr, MU_SUN, perturbation_model)
      if r_final is None:
         print("Falha na convergência desta iteração. Tentando novamente...")
         rho_initial *= 0.7
         continue
      rho_new = np.array([np.linalg.norm(r_final - R[i]) for i in range(len(R))])
      delta_rho = np.max(np.abs(rho_new - rho_initial))
      print(f"Δρ = {delta_rho:.6f} AU")
      if delta_rho < 1e-6:
         print("Convergência alcançada!")
         break
      rho_initial = rho_new
   if r_final is None:
      print("Falha ao encontrar órbita válida após todas as iterações.")
      return
   a, e, i, Omega, omega, theta = calculate_orbital_elements(r_final, v_final, MU_SUN)
   def perturbation_model(t, r, v):
      return solar_system_perturbations(t, r, v, t_corr[0])
   r_pred, _ = propagate_orbit(r_final, v_final, t_corr, t_corr[0], MU_SUN, perturbation_model)
   residuos_ang = []
   for j in range(len(t)):
      rho_vec = r_pred[j] - R[j]
      rho_norm = np.linalg.norm(rho_vec)
      rho_pred = rho_vec / rho_norm
      ang = np.arccos(np.clip(np.dot(rho_pred, rho_hat[j]), -1.0, 1.0))
      residuos_ang.append(np.degrees(ang) * 3600)
   rms_residuo = np.sqrt(np.mean(np.array(residuos_ang)**2))
   print(f"\nr_final = {r_final}, |r| = {np.linalg.norm(r_final):.6f} AU")
   print(f"v_final = {v_final}, |v| = {np.linalg.norm(v_final):.6f} AU/dia")
   print(f"Resíduo RMS: {rms_residuo:.3f} segundos de arco")

   print(f"\n### Elementos Orbitais Finais para ({name}) ###")
   print(f"a = {a:.6f} UA")
   print(f"e = {e:.6f}")
   print(f"i = {i:.6f}°")
   print(f"Ω = {Omega:.6f}°")
   print(f"ω = {omega:.6f}°")
   print(f"θ = {theta:.6f}°")
   

# --------------------------------------------------------------------------------------------------------
def gauss_iod_method(R, rho_hat, t, mu=0.000295912208):
    """
    Gauss Initial Orbit Determination method, optimized for short arcs
    
    Args:
        R (list): Lista de vetores posição da Terra [r1, r2, r3] (AU)
        rho_hat (list): Lista de vetores de direção unitários [rho_hat1, rho_hat2, rho_hat3]
        t (list): Lista de tempos de observação [t1, t2, t3]
        mu (float): Parâmetro gravitacional (AU^3/day^2)
    
    Returns:
        np.array: Vetor posição inicial r2 no tempo t2
        np.array: Vetor velocidade inicial v2 no tempo t2
    """

    if len(R) < 3:
        print("Método de Gauss requer pelo menos 3 observações")
        return None, None
    
    if len(R) > 3:
        indices = [0, len(R) // 2, -1]
        R = [R[i] for i in indices]
        rho_hat = [rho_hat[i] for i in indices]
        t = [t[i] for i in indices]
    
    tau1 = (t[0] - t[1]).total_seconds() / 86400.0
    tau3 = (t[2] - t[1]).total_seconds() / 86400.0

    # Coeficientes de Lagrange mais precisos
    def better_f_and_g(tau, r_mag, mu):
        """Coeficientes de Lagrange mais precisos para arcos curtos"""
        u = mu / r_mag**3
        f = 1 - 0.5 * u * tau**2 + (1/24) * u**2 * tau**4
        g = tau - (1/6) * u * tau**3 + (1/120) * u**2 * tau**5
        return f, g
    
    r2_mag_guess = 1.5
    f1, g1 = better_f_and_g(tau1, r2_mag_guess, mu)
    f3, g3 = better_f_and_g(tau3, r2_mag_guess, mu)
    D0 = np.column_stack([rho_hat[0], rho_hat[1], rho_hat[2]])
    D = np.zeros((3, 3))
    D[0, 0] = np.linalg.det(np.column_stack([rho_hat[1], rho_hat[2], R[1]]))
    D[0, 1] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[2], R[1]]))
    D[0, 2] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[1], R[1]]))
    D[1, 0] = np.linalg.det(np.column_stack([rho_hat[1], rho_hat[2], R[0]]))
    D[1, 1] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[2], R[0]]))
    D[1, 2] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[1], R[0]]))
    D[2, 0] = np.linalg.det(np.column_stack([rho_hat[1], rho_hat[2], R[2]]))
    D[2, 1] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[2], R[2]]))
    D[2, 2] = np.linalg.det(np.column_stack([rho_hat[0], rho_hat[1], R[2]]))
    D0_det = np.linalg.det(D0)

    if abs(D0_det) < 1e-8: 
        print("Alerta: Observações quase coplanares. Tentando correção...")
        D0_det = np.sign(D0_det) * 1e-8 

    A = np.zeros((3, 3))
    b = np.zeros(3)
    A[0, 0] = -D[0, 0] / D0_det
    A[0, 1] = D[0, 1] / D0_det
    A[0, 2] = -D[0, 2] / D0_det
    A[1, 0] = -D[1, 0] / D0_det
    A[1, 1] = D[1, 1] / D0_det
    A[1, 2] = -D[1, 2] / D0_det
    A[2, 0] = -D[2, 0] / D0_det
    A[2, 1] = D[2, 1] / D0_det
    A[2, 2] = -D[2, 2] / D0_det
    b[0] = -np.dot(rho_hat[1], R[0])
    b[1] = -np.dot(rho_hat[1], R[1])
    b[2] = -np.dot(rho_hat[1], R[2])
    starting_points = [1.0, 1.5, 2.0, 2.5]
    best_solution = None
    best_delta_v = float('inf')
    
    for start_attempt, rho2_initial in enumerate(starting_points):
        try:
            A_reg = A + np.eye(3) * 1e-5

            if start_attempt == 0:
                try:
                    rho = np.linalg.solve(A_reg, b)
                    print(f"Distâncias calculadas: {rho[0]:.4f}, {rho[1]:.4f}, {rho[2]:.4f} UA")
                    if all(0.1 <= r <= 10 for r in rho):
                        r1 = R[0] + rho[0] * rho_hat[0]
                        r2 = R[1] + rho[1] * rho_hat[1]
                        r3 = R[2] + rho[2] * rho_hat[2]
                        r2_mag = np.linalg.norm(r2)
                        f1, g1 = better_f_and_g(tau1, r2_mag, mu)
                        f3, g3 = better_f_and_g(tau3, r2_mag, mu)
                        v2_from_1 = (r1 - f1 * r2) / g1
                        v2_from_3 = (r3 - f3 * r2) / g3
                        delta_v = np.linalg.norm(v2_from_1 - v2_from_3)
                        if delta_v < 0.01: 
                            v = (v2_from_1 + v2_from_3) / 2
                            r = r2
                            print(f"Solução direta com boa consistência (delta_v = {delta_v:.6f} UA/dia)")
                            if delta_v < best_delta_v:
                                best_solution = (r, v, delta_v)
                                best_delta_v = delta_v
                    else:
                        print("Solução direta produziu distâncias não razoáveis, tentando método iterativo...")
                except np.linalg.LinAlgError:
                    print("Erro na solução direta, tentando método iterativo...")
            
            rho2 = rho2_initial
            best_iteration_delta_v = float('inf')
            best_iteration_result = None
            
            for iteration in range(10): 
                r2 = R[1] + rho2 * rho_hat[1]
                r2_mag = np.linalg.norm(r2)
                f1, g1 = better_f_and_g(tau1, r2_mag, mu)
                f3, g3 = better_f_and_g(tau3, r2_mag, mu)
                if abs(g1) < 1e-10 or abs(g3) < 1e-10:
                    print(f"Coeficientes g muito pequenos: g1={g1:.2e}, g3={g3:.2e}. Ajustando...")
                    g1 = np.sign(g1) * max(abs(g1), 1e-10)
                    g3 = np.sign(g3) * max(abs(g3), 1e-10)
                rho1 = np.dot(R[1] - R[0], rho_hat[1]) / np.dot(rho_hat[0], rho_hat[1]) + rho2 * np.dot(rho_hat[1], rho_hat[0])
                rho3 = np.dot(R[1] - R[2], rho_hat[1]) / np.dot(rho_hat[2], rho_hat[1]) + rho2 * np.dot(rho_hat[1], rho_hat[2])
                rho1 = np.clip(rho1, 0.1, 10.0)
                rho3 = np.clip(rho3, 0.1, 10.0)
                r1 = R[0] + rho1 * rho_hat[0]
                r3 = R[2] + rho3 * rho_hat[2]
                try:
                    v2_from_1 = (r1 - f1 * r2) / g1
                    v2_from_3 = (r3 - f3 * r2) / g3
                    if not np.all(np.isfinite(v2_from_1)) or not np.all(np.isfinite(v2_from_3)):
                        raise ValueError("Velocidades infinitas detectadas")
                    v2 = (v2_from_1 + v2_from_3) / 2
                    delta_v = np.linalg.norm(v2_from_1 - v2_from_3)
                    if delta_v < best_iteration_delta_v:
                        best_iteration_delta_v = delta_v
                        best_iteration_result = (r2.copy(), v2.copy(), delta_v)
                    if delta_v < 0.0001: 
                        print(f"Convergiu em {iteration+1} iterações!")
                        break
                    dot_product = np.dot(v2_from_3 - v2_from_1, r2)
                    adjustment = 0.15 * (1.0 / (iteration + 1))
                    rho2_new = rho2 * (1 + adjustment * np.tanh(dot_product * min(0.1, delta_v)))
                    if not np.isfinite(rho2_new) or abs(rho2_new - rho2) > 0.5:
                        print("Ajuste muito grande, limitando...")
                        rho2_new = rho2 + np.sign(rho2_new - rho2) * 0.1
                    rho2 = np.clip(rho2_new, 0.1, 10.0)
                except Exception as e:
                    print(f"Erro no cálculo da velocidade: {e}")
                    break
            
            if best_iteration_result is not None:
                if best_iteration_delta_v < best_delta_v:
                    best_solution = best_iteration_result
                    best_delta_v = best_iteration_delta_v
                    print(f"Nova melhor solução encontrada (delta_v = {best_delta_v:.6f} UA/dia)")
        except Exception as e:
            print(f"Erro na tentativa {start_attempt+1}: {e}")
    
    if best_solution is None:
        print("Não foi possível encontrar uma solução estável.")
        return None, None
    
    r, v, delta_v = best_solution
    try:
        a, e, inc, omega, w, theta = calculate_orbital_elements(r, v, mu)
        if not np.isfinite(a) or not np.isfinite(e) or not np.isfinite(inc):
            print("Elementos orbitais infinitos detectados, ajustando...")
            if not np.isfinite(a):
                if a > 0:  
                    a = 100  
                else:  
                    a = -100  
            if not np.isfinite(e):
                e = 0.99 if a > 0 else 1.01 
            if not np.isfinite(inc):
                inc = 0 
        if e > 1000:
            print(f"Excentricidade extremamente alta ({e:.4f}), ajustando para um valor mais razoável")
            e = np.clip(e, 0, 10.0)
        print(f"Solução pelo método de Gauss: a = {a:.4f} UA, e = {e:.4f}, i = {inc:.4f}°")
        print(f"Velocidade orbital: {np.linalg.norm(v)*1731:.1f} km/h")
        orbit_valid = False
        if a > 0 and a < 100 and e < 0.99:
            print("Órbita elíptica razoável detectada")
            orbit_valid = True
        elif a < 0 and e > 1.0 and e < 10:
            print("Órbita hiperbólica detectada - possível objeto em trajetória de escape")
            orbit_valid = True
        elif abs(e - 1.0) < 0.01 and a > 100:
            print("Órbita aproximadamente parabólica detectada")
            orbit_valid = True
        if orbit_valid:
            return r, v
        else:
            print(f"Órbita não razoável: a = {a:.4f}, e = {e:.4f}")
            if a > 100:
                print("Semieixo maior muito grande - possível objeto muito distante ou erro numérico")
            elif a < -100:
                print("Semieixo maior muito negativo - trajetória hiperbólica extrema ou erro numérico")
            elif e >= 0.99 and e < 1.5:
                print("Excentricidade próxima ou pouco acima de 1 - verificar se é um cometa")
            elif e >= 1.5:
                print("Excentricidade muito alta - provável erro numérico")
            if best_delta_v > 0.01:
                print(f"Alta inconsistência entre velocidades (delta_v = {best_delta_v:.6f} UA/dia)")
                print("Provável causa: arco muito curto ou observações imprecisas")
            if best_delta_v < 0.1: 
                print("Retornando solução de melhor esforço, mas use com cautela!")
                return r, v
            return None, None
    except Exception as e:
        print(f"Erro no cálculo de elementos orbitais: {e}")
        print("Retornando vetores estado, mas elementos orbitais não puderam ser calculados")
        return r, v
    