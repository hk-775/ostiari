import { useState, useEffect } from "react";
import { User } from "../stores/authStore";
import { UserPlus, Trash2, Users as UsersIcon } from "lucide-react";
import { fetchAPI } from "../lib/api";

const ROLE_BADGES: Record<string, string> = {
  admin: "bg-violet-100 text-violet-700 border-violet-200",
  editor: "bg-sky-100 text-sky-700 border-sky-200",
  viewer: "bg-stone-100 text-stone-600 border-stone-200",
};

export function Users() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Registration form
  const [showForm, setShowForm] = useState(false);
  const [formEmail, setFormEmail] = useState("");
  const [formName, setFormName] = useState("");
  const [formPassword, setFormPassword] = useState("");
  const [formRole, setFormRole] = useState("viewer");
  const [formError, setFormError] = useState("");
  const [formLoading, setFormLoading] = useState(false);

  const fetchUsers = async () => {
    try {
      const data = await fetchAPI<User[]>("/api/auth/users");
      setUsers(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError("");
    setFormLoading(true);
    try {
      await fetchAPI<User>("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
          email: formEmail,
          name: formName,
          password: formPassword,
          role: formRole,
        }),
      });
      setFormEmail("");
      setFormName("");
      setFormPassword("");
      setFormRole("viewer");
      setShowForm(false);
      fetchUsers();
    } catch (err: any) {
      setFormError(err.message);
    } finally {
      setFormLoading(false);
    }
  };

  const handleDelete = async (userId: string) => {
    if (!confirm("Are you sure you want to delete this user?")) return;
    try {
      await fetchAPI(`/api/auth/users/${userId}`, { method: "DELETE" });
      fetchUsers();
    } catch (err: any) {
      setError(err.message);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-sm text-stone-400">Loading users...</p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">User Management</h1>
          <p className="mt-1 text-sm text-stone-500">Manage access to the Ostiari control plane</p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="inline-flex items-center gap-2 rounded-xl border border-violet-200 bg-violet-50 px-4 py-2.5 text-sm font-medium text-violet-700 transition hover:bg-violet-100 hover:border-violet-300"
        >
          <UserPlus className="h-4 w-4" />
          Register User
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      {/* Registration Form */}
      {showForm && (
        <div className="card p-6">
          <h2 className="text-sm font-semibold text-stone-700 mb-4">Register New User</h2>
          {formError && (
            <div className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {formError}
            </div>
          )}
          <form onSubmit={handleRegister} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Email</label>
              <input
                type="email"
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
                className="input w-full"
                placeholder="user@company.com"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Name</label>
              <input
                type="text"
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                className="input w-full"
                placeholder="Full name"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Password</label>
              <input
                type="password"
                value={formPassword}
                onChange={(e) => setFormPassword(e.target.value)}
                className="input w-full"
                placeholder="Password"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Role</label>
              <select
                value={formRole}
                onChange={(e) => setFormRole(e.target.value)}
                className="input w-full"
              >
                <option value="admin">Admin</option>
                <option value="editor">Editor</option>
                <option value="viewer">Viewer</option>
              </select>
            </div>
            <div className="flex items-end">
              <button
                type="submit"
                disabled={formLoading}
                className="btn-primary w-full disabled:opacity-50"
              >
                {formLoading ? "Creating..." : "Create"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Users List */}
      <div className="card">
        <div className="card-header flex items-center gap-2">
          <UsersIcon className="h-4 w-4 text-stone-500" />
          <h2 className="text-sm font-semibold text-stone-700">
            All Users ({users.length})
          </h2>
        </div>
        <div className="divide-y divide-stone-100">
          {users.length === 0 && (
            <p className="px-6 py-10 text-center text-sm text-stone-400">
              No users found.
            </p>
          )}
          {users.map((user) => (
            <div key={user.id} className="flex items-center justify-between px-6 py-4 transition hover:bg-stone-50">
              <div className="flex items-center gap-4">
                <div className="flex h-9 w-9 items-center justify-center rounded-full bg-stone-100 text-sm font-medium text-stone-600">
                  {user.name?.charAt(0)?.toUpperCase() || user.email.charAt(0).toUpperCase()}
                </div>
                <div>
                  <p className="text-sm font-medium text-stone-800">{user.name}</p>
                  <p className="text-xs text-stone-400">{user.email}</p>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <span className={`inline-flex items-center rounded-lg border px-2.5 py-1 text-xs font-medium ${ROLE_BADGES[user.role] || ROLE_BADGES.viewer}`}>
                  {user.role}
                </span>
                <button
                  onClick={() => handleDelete(user.id)}
                  className="rounded-lg p-2 text-stone-400 transition hover:bg-rose-50 hover:text-rose-600"
                  title="Delete user"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
